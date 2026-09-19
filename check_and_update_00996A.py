"""
00996A ETF Holdings Daily Checker & Updater (兆豐台灣豐收主動式ETF)

Logic (類型 H：megafunds.com.tw 靜態 HTML):
1. Fetch static HTML from https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id=23
2. Parse holdings from div-based layout (fund-info content-list-1 blocks):
   股票代號 / 股票名稱 / 股數 / 持股權重
3. Validate page data date (資料來源：兆豐投信，YYYY/MM/DD) equals today
4. AUM: 淨資產價值 + 在外流通單位數 from the same page
5. Compare with previous day's holdings, fetch prices via yfinance
6. Generate data_00996A.json, append to Google Sheets, send Telegram
"""

import json
import os
import re
import sys
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序

# --------------- Config ---------------
HOLDINGS_URL = "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id=23"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00996A.json"
ETF_CODE = "00996A"
MANAGER = "王仲良"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00996A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00996A", name="主動兆豐台灣豐收", manager="王仲良")


# --------------- Helpers ---------------

def fetch_page():
    """Fetch the megafunds product page HTML once (holdings + AUM + date all on it)."""
    req = urllib.request.Request(
        HOLDINGS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_page(html):
    """
    Parse holdings, data date, and AUM from megafunds HTML.

    Returns (holdings, page_date_str, aum_ntd, units)
      holdings      : [{code, name, shares, weight}, ...]
      page_date_str : "YYYY-MM-DD" from 「資料來源：兆豐投信，YYYY/MM/DD」
      aum_ntd       : 淨資產價值 (int)
      units         : 在外流通單位數 (int)
    """
    # 資料日期
    page_date_str = ""
    m = re.search(r'資料來源：兆豐投信，(\d{4}/\d{1,2}/\d{1,2})', html)
    if m:
        page_date_str = m.group(1).replace("/", "-")
        # normalize zero padding
        parts = page_date_str.split("-")
        page_date_str = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    # 持股（PC 版 content-list-1 區塊；手機版為 item-content 不抓避免重複）
    holdings = []
    row_pattern = re.compile(
        r'<div class="fund-info content-list-1">\s*'
        r'<div class="fund-content">([^<]+)</div>\s*'
        r'<div class="fund-content">([^<]+)</div>\s*'
        r'<div class="fund-content txt-right">([^<]+)</div>\s*'
        r'<div class="fund-content txt-right">([^<]+)</div>',
        re.DOTALL,
    )
    for m in row_pattern.finditer(html):
        code = m.group(1).strip()
        if not re.fullmatch(r'\d{4,6}[A-Z]?', code):
            continue
        name = m.group(2).strip()
        try:
            shares = int(m.group(3).replace(",", "").strip())
            weight = float(m.group(4).replace("%", "").strip())
        except ValueError:
            continue
        holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})

    # AUM
    aum_ntd, units = 0, 0
    m = re.search(r'淨資產價值</div>\s*<div class="si-amount">\s*([\d,]+)', html)
    if m:
        aum_ntd = int(m.group(1).replace(",", ""))
    m = re.search(r'在外流通單位數</div>\s*<div class="si-amount">\s*([\d,]+)', html)
    if m:
        units = int(m.group(1).replace(",", ""))

    return holdings, page_date_str, aum_ntd, units


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: x["diffShares"])
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00996A 兆豐台灣豐收 持股更新",
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
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['shares'])}（0% → {h['todayWeight']}%）")
    if removed:
        lines.append("\n出清持股：")
        for h in removed:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(-h.get('prevShares', 0))}")
    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


# --------------- Main ---------------

def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    data_date_str = run_date.strftime("%Y-%m-%d")

    log.info(f"=== 00996A Check & Update started ===")
    log.info(f"  Run date / Data date: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    try:
        html = fetch_page()
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        send_telegram(f"⏳ 00996A 兆豐台灣豐收 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    today_holdings, page_date_str, aum_ntd, units = parse_page(html)
    log.info(f"Parsed {len(today_holdings)} holdings, page date: {page_date_str}, "
             f"AUM: {aum_ntd:,} NTD, Units: {units:,}")

    # 日期驗證：官網頁面日期必須等於今天，否則視為尚未更新
    # （防止排程延遲跨日、或官網尚未換日時抓到舊資料）
    if not today_holdings or page_date_str != data_date_str:
        log.info(f"Page date {page_date_str} != today {data_date_str} (or no holdings). Not updated yet.")
        send_telegram(f"⏳ 00996A 兆豐台灣豐收 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
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
