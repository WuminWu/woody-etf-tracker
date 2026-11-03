"""
00416A ETF Holdings Daily Checker & Updater (主動台新臺灣中小)

Logic:
1. Fetch static HTML from https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00416A
2. Parse holdings table: 股票代號, 名稱, 股數, 持股權重
   (stock codes have " TT" suffix — stripped on parse)
   Holdings date = 頁面「YYYY/M/D每基數實際申購總價金」的日期（申購買回清單日 PUB_DATE 是 T+1，
   清單內容是 T 日收盤後部位）；不是今天 → 官網尚未更新，不寫入（同站的 00987A 直接用今天，無此防護）
3. Compare with previous day's holdings
4. Fetch stock prices via yfinance
5. Generate data_00416A.json
6. Push to GitHub
7. Send Telegram notification
"""

import json
import os
import sys
import logging
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序

# --------------- Config ---------------
HOLDINGS_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00416A"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00416A.json"
ETF_CODE = "00416A"
MANAGER = "沈建宏"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00416A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- HTML Parser ---------------

class HoldingsTableParser(HTMLParser):
    """Parse the holdings table from tsit.com.tw ETF detail page.

    Scans ALL table rows — any row whose first cell looks like a stock code
    (4-digit number optionally followed by ' TT') is treated as a holding.
    """

    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_td = False
        self.current_row = []
        self.current_cell = ""
        self.holdings = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_tr = True
            self.current_row = []
        if self.in_tr and tag == "td":
            self.in_td = True
            self.current_cell = ""

    def handle_endtag(self, tag):
        if tag == "tr":
            self.in_tr = False
            if len(self.current_row) >= 4:
                self._process_row(self.current_row)
        if self.in_tr and tag == "td":
            self.in_td = False
            self.current_row.append(self.current_cell.strip())

    def handle_data(self, data):
        if self.in_td:
            self.current_cell += data

    def _process_row(self, row):
        import re
        try:
            code = row[0].replace(" TT", "").strip()
            # Must be a 4-digit stock code
            if not re.fullmatch(r'\d{4}', code):
                return
            name = row[1].strip()
            shares_str = row[2].replace(",", "").strip()
            weight_str = row[3].replace("%", "").strip()
            shares = int(float(shares_str))
            weight = float(weight_str)
            self.holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
        except Exception:
            pass


# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, trade_amount, change_text, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00416A", name="主動台新臺灣中小", manager="沈建宏",
                 aum_derive_check=True,   # 同站 00987A 來源張數偶爾錯，與 規模/淨值 反推值互相校驗
                 ipo_date="2026-11-03", ipo_price=10.0, yf_suffix=".TW")


# --------------- Helpers ---------------

def fetch_page():
    log.info(f"Fetching {HOLDINGS_URL} ...")
    try:
        req = urllib.request.Request(
            HOLDINGS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None


def parse_holdings(html):
    parser = HoldingsTableParser()
    parser.feed(html)
    log.info(f"Parsed {len(parser.holdings)} holdings from page")
    return parser.holdings


def parse_holdings_date(html):
    """持股日 = 「2026/11/2每基數實際申購總價金」的日期（YYYY-MM-DD）；找不到回傳 None。"""
    import re
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})\s*每基數實際申購總價金', html)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_aum(html):
    import re
    try:
        aum_ntd, units = 0, 0
        # Find 基金淨資產價值(元) value
        m = re.search(r'基金淨資產價值.*?TWD\s*([\d,]+)', html, re.DOTALL)
        if m:
            aum_ntd = int(m.group(1).replace(",", ""))
        # Find 已發行受益權單位總數 value
        m2 = re.search(r'已發行受益權單位總數.*?<td[^>]*>\s*([\d,]+)', html, re.DOTALL)   # 數字前有換行縮排
        if m2:
            units = int(m2.group(1).replace(",", ""))
        log.info(f"AUM from HTML: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM fetch from HTML failed: {e}")
        return 0, 0


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: -abs(trade_amount(x, meta)))
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: -abs(trade_amount(x, meta)))
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00416A 主動台新臺灣中小 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
        format_trade_line(wrapper),   # 💹 當日買超/賣超/淨額（etf_core，與日報同一套算法）
    ]
    if added:
        lines.append("\n新增持股：")
        for h in added:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'added')}")
    if removed:
        lines.append("\n出清持股：")
        for h in removed:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'removed')}")
    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'up')}")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'down')}")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    # 收盤後官網換成「T+1 申購買回清單」＝今天收盤後的持股；預期持股日就是今天
    data_date_str = run_date.strftime("%Y-%m-%d")

    log.info(f"=== 00416A Check & Update started ===")
    log.info(f"  Run date / Data date: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    html = fetch_page()
    today_holdings = parse_holdings(html) if html else None
    aum_ntd, units = parse_aum(html) if html else (0, 0)
    page_date = parse_holdings_date(html) if html else None
    if today_holdings and page_date != data_date_str:
        log.info(f"官網持股日 {page_date} ≠ {data_date_str}，尚未更新")
        today_holdings = None
    if not today_holdings:
        log.error("No holdings fetched. Will retry next hour.")
        send_telegram(f"⏳ 00416A 主動台新臺灣中小 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
