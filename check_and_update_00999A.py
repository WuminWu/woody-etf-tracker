"""
00999A ETF Holdings Daily Checker & Updater (主動野村臺灣高息)

Logic:
1. Call POST /API/ETFAPI/api/Fund/GetFundAssets with SearchDate=prev_trading_day
2. Parse holdings JSON directly (no Excel/Playwright needed)
3. Compare with previous day's holdings
4. Fetch stock prices via yfinance
5. Generate data_00999A.json
6. Push to GitHub
7. Send Telegram notification
"""

import json
import os
import sys
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from etf_registry import site_link   # 網站網址（GitHub Pages 已停用，舊網址 404）

# --------------- Config ---------------
API_URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00999A.json"
ETF_CODE = "00999A"
MANAGER = "游景德"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00999A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, trade_amount, change_text, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00999A", name="主動野村臺灣高息", manager="游景德")


# --------------- Helpers ---------------

def fetch_holdings(date_str):
    """Fetch holdings from Nomura API. date_str: YYYY-MM-DD"""
    payload = json.dumps({"FundID": ETF_CODE, "SearchDate": date_str}).encode()
    req = urllib.request.Request(
        API_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    log.info(f"Fetching holdings for {date_str} ...")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        table = result["Entries"]["Data"]["Table"][0]
        nav_date = table.get("NavDate", "").replace("/", "-")
        rows = table["Rows"]
        log.info(f"Got {len(rows)} stocks, NavDate={nav_date}")
        holdings = []
        for row in rows:
            code, name, shares_str, weight_str = row[0], row[1], row[2], row[3]
            try:
                holdings.append({
                    "code": str(code).strip(),
                    "name": str(name).strip(),
                    "shares": int(str(shares_str).replace(",", "")),
                    "weight": float(str(weight_str).replace("%", "")),
                })
            except Exception:
                pass
        # Also extract AUM from FundAsset
        aum_ntd, units = 0, 0
        try:
            fa = result["Entries"]["Data"]["FundAsset"]
            aum_ntd = int(str(fa.get("Aum", "0")).replace(",", ""))
            units = int(str(fa.get("Units", "0")).replace(",", ""))
            log.info(f"AUM: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        except Exception as e:
            log.warning(f"FundAsset parse failed: {e}")
        return holdings, aum_ntd, units, nav_date
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None, 0, 0, ""


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: -abs(trade_amount(x, meta)))
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: -abs(trade_amount(x, meta)))
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00999A 主動野村臺灣高息 持股更新",
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
    lines.append(site_link(CFG.code))   # 網址單一來源（etf_registry），直接開到這一檔
    return "\n".join(lines)


def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    run_date_str = run_date.strftime("%Y-%m-%d")
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")

    log.info(f"=== 00999A Check & Update started ===")
    log.info(f"  Run date:  {run_date_str}")
    log.info(f"  Prev date: {data_date_str}")

    # Nomura API returns NavDate = actual data date.
    # Query today first; the API may already have today's holdings.
    today_holdings, aum_ntd, units, nav_date = fetch_holdings(run_date_str)
    actual_date_str = nav_date if nav_date else run_date_str

    if not today_holdings or not nav_date:
        log.warning("Today's Nomura API data not available. Falling back to previous trading day.")
        if holdings_exist_for(CFG, data_date_str):
            log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
            return
        today_holdings, aum_ntd, units, nav_date = fetch_holdings(data_date_str)
        actual_date_str = data_date_str
        if not today_holdings:
            log.error("No holdings fetched. Will retry next hour.")
            send_telegram(f"⏳ 00999A 主動野村臺灣高息 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
            return
    else:
        log.info(f"NavDate from API: {actual_date_str}")
        if holdings_exist_for(CFG, actual_date_str):
            log.info(f"Holdings for {actual_date_str} already exist. Nothing to do.")
            return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{actual_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, actual_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, actual_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
