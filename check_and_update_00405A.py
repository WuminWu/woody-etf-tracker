"""
00405A ETF Holdings Daily Checker & Updater (主動富邦台灣龍耀)

資料來源類型 K：富邦投信 ETF 投資網「基金資產」頁（伺服器端渲染 HTML，純 HTTP 即可）
  https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=00405A
  - 持股表欄位：股票代碼 / 名稱 / 股數 / 金額 / 權重(%)
  - 頁面標示「資料日期：YYYY/MM/DD」「基金淨資產(新台幣)」「基金在外流通單位數(單位)」
  - 以「資料日期 == 今天」驗證，避免假日 / 官網未更新時寫入舊資料

注意：00405A 於 2026/6/9 掛牌（IPO 價 10 元），update_prices.py 的 IPO_BASELINE
已設定掛牌年以 IPO 價計算 YTD。
"""

import json
import os
import sys
import re
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序

# --------------- Config ---------------
HOLDINGS_URL = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=00405A"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00405A.json"
ETF_CODE = "00405A"
ETF_NAME = "主動富邦台灣龍耀"
MANAGER = "高晧欣"
IPO_DATE = "2026-06-09"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00405A.log", encoding="utf-8"),
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
CFG = FundConfig(code="00405A", name="主動富邦台灣龍耀", manager="高晧欣",
                 ipo_date="2026-06-09", ipo_price=10.0)


# --------------- Fetch & parse ---------------

def _fetch_html():
    req = urllib.request.Request(
        HOLDINGS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _text_lines(html):
    """去標籤後的純文字行（label 與值常相鄰兩行）。"""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "\n", t)
    return [l.strip() for l in t.split("\n") if l.strip()]


def _value_after(lines, label):
    for i, l in enumerate(lines):
        if label in l:
            # 同行帶值（label：value）或下一行
            m = re.search(re.escape(label) + r"[：:]\s*([\d,/\.]+)", l)
            if m:
                return m.group(1)
            for nxt in lines[i + 1:i + 3]:
                m2 = re.fullmatch(r"[\d,\.]+", nxt)
                if m2:
                    return nxt
    return None


def fetch_data():
    """回傳 (holdings, data_date_str, aum_ntd, units) 或 (None, ...) 失敗。"""
    try:
        html = _fetch_html()
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None, None, 0, 0

    # 持股表
    holdings = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
        if len(cells) >= 5 and re.fullmatch(r"\d{4,6}", cells[0]):
            try:
                holdings.append({
                    "code": cells[0],
                    "name": cells[1],
                    "shares": int(cells[2].replace(",", "")),
                    "weight": float(cells[4].replace("%", "").replace(",", "")),
                })
            except ValueError:
                continue

    lines = _text_lines(html)
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{2}/\d{2})", html)
    data_date_str = m.group(1).replace("/", "-") if m else None

    aum_str = _value_after(lines, "基金淨資產(新台幣)")
    units_str = _value_after(lines, "基金在外流通單位數(單位)")
    aum_ntd = int(aum_str.replace(",", "")) if aum_str else 0
    units = int(units_str.replace(",", "")) if units_str else 0

    log.info(f"Parsed {len(holdings)} holdings, date={data_date_str}, "
             f"AUM={aum_ntd:,} ({aum_ntd/1e8:.2f}億), units={units:,}")
    return holdings, data_date_str, aum_ntd, units


# --------------- Shared helpers (同其他台股腳本) ---------------

def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: x["diffShares"])
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 {ETF_CODE} {ETF_NAME} 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
        format_trade_line(wrapper),   # 💰 當日買超/賣超/淨額（etf_core，與日報同一套算法）
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
    run_date_str = datetime.now(timezone(timedelta(hours=8))).date().strftime("%Y-%m-%d")
    log.info(f"=== {ETF_CODE} Check & Update started ===")
    log.info(f"  Run date: {run_date_str}")

    holdings, data_date_str, aum_ntd, units = fetch_data()
    if not holdings:
        log.error("No holdings fetched. Will retry next run.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {run_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 資料日期驗證：官網日期須等於今天，否則視為尚未更新
    if data_date_str != run_date_str:
        log.info(f"官網資料日期 {data_date_str} != 今天 {run_date_str}，尚未更新。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新（官網仍為 {data_date_str}）\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    if os.path.exists(json_path):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
