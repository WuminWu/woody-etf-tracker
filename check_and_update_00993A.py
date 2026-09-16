"""
00993A ETF Holdings Daily Checker & Updater (主動安聯台灣)

Data source: Allianz official site - intercepts GetFundAssets API via Playwright
https://etf.allianzgi.com.tw/etf-info/E0002?tab=4
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段

# --------------- Config ---------------
ETF_CODE = "00993A"
ETF_NAME = "主動安聯台灣"
MANAGER = "安聯投信"
PAGE_URL = "https://etf.allianzgi.com.tw/etf-info/E0002?tab=4"
API_KEYWORD = "GetFundAssets"
HOLDINGS_DIR = "holdings"
DATA_FILE = f"data_{ETF_CODE}.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))


from tw_calendar import TW_MARKET_HOLIDAYS   # 單一來源：台股休市日（tw_calendar.py）

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00993A", name="主動安聯台灣", manager="安聯投信",
                 units_are_zhang=True, aum_yf_fallback=True, aum_carry_prev=False,
                 has_futures=True)   # 持股含 TX 台指期，不可當個股統計


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"check_and_update_{ETF_CODE}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- Allianz API Fetcher ---------------

def fetch_fund_assets():
    """Navigate to Allianz ETF page and intercept GetFundAssets + GetFundDetail API responses."""
    captured = {}

    def handle_response(response):
        url = response.url
        if API_KEYWORD in url:
            try:
                captured["data"] = response.json()
                log.info(f"Captured holdings API: {url}")
            except Exception as e:
                log.warning(f"Failed to parse holdings response: {e}")
        elif "GetFundDetail" in url:
            try:
                body = response.json()
                manager = body.get("Entries", {}).get("CManager", "")
                if manager:
                    captured["manager"] = manager
                    log.info(f"Captured manager: {manager}")
            except Exception as e:
                log.warning(f"Failed to parse GetFundDetail response: {e}")

    log.info(f"Launching Playwright to fetch {PAGE_URL} ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="zh-TW",
        )
        page = context.new_page()
        page.on("response", handle_response)
        try:
            # Navigate to tab=1 first to trigger GetFundDetail (manager info)
            page.goto(PAGE_URL.replace("tab=4", "tab=1"), wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4000)
            # Then navigate to tab=4 for holdings
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(8000)
        except Exception as e:
            log.warning(f"Page load issue (non-fatal): {e}")
        browser.close()

    return captured.get("data"), captured.get("manager")


def parse_fund_assets(raw_data):
    """
    Parse Allianz GetFundAssets response.
    Returns (holdings_list, aum_ntd, units_zhang, nav, pcf_date_str)
    """
    try:
        entries = raw_data["Entries"]["Data"]
        fa = entries["FundAsset"]
        tables = entries["Table"]
    except (KeyError, TypeError) as e:
        log.error(f"Unexpected API structure: {e}")
        return None, 0, 0, 0.0, ""

    # FundAsset fields
    try:
        aum_ntd = float(str(fa.get("Aum", "0")).replace(",", ""))
        units_raw = float(str(fa.get("Units", "0")).replace(",", ""))
        units_zhang = int(units_raw // 1000)
        nav = float(fa.get("Nav", 0))
        # Prefer NavDate (actual data date) over PCFDate (future creation/redemption basket date)
        pcf_date_str = str(fa.get("NavDate", fa.get("PCFDate", ""))).replace("/", "-")
        log.info(f"AUM: {aum_ntd:,.0f} NTD, Units: {units_zhang:,}張, NAV: {nav}, PCF Date: {pcf_date_str}")
    except Exception as e:
        log.warning(f"FundAsset parse error: {e}")
        aum_ntd, units_zhang, nav, pcf_date_str = 0, 0, 0.0, ""

    holdings = []

    # Table[1] = stocks: each row is [rank, code, name, shares, weight%]
    stock_rows = tables[1]["Rows"] if len(tables) > 1 else []
    for row in stock_rows:
        try:
            if len(row) < 5:
                continue
            code_raw = str(row[1]).strip()
            name = str(row[2]).strip()
            shares = int(float(str(row[3]).replace(",", "")))
            weight = float(str(row[4]).replace("%", "").replace(",", ""))
            # Extract numeric stock code (4-6 digits)
            m = re.match(r'^(\d{4,6})', code_raw)
            if not m or weight <= 0:
                continue
            code = m.group(1)
            holdings.append({
                "code": code, "name": name,
                "shares": shares, "weight": weight,
                "is_futures": False,
            })
        except Exception:
            continue

    # Table[2] = futures: each row is [rank, code, name, contracts, weight%, expiry]
    futures_rows = tables[2]["Rows"] if len(tables) > 2 else []
    for row in futures_rows:
        try:
            if len(row) < 5:
                continue
            code_raw = str(row[1]).strip()
            name = str(row[2]).strip()
            contracts = int(float(str(row[3]).replace(",", "")))
            weight = float(str(row[4]).replace("%", "").replace(",", ""))
            if weight <= 0:
                continue
            holdings.append({
                "code": code_raw, "name": name,
                "shares": contracts, "weight": weight,
                "is_futures": True,
            })
        except Exception:
            continue

    log.info(f"Parsed {len(holdings)} holdings ({len(stock_rows)} stocks, {len(futures_rows)} futures)")
    return holdings, aum_ntd, units_zhang, nav, pcf_date_str


# --------------- Helpers ---------------

def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    # 期貨（TX 台指期貨）不是個股：單位是「口」不是「張」、也沒有股價，
    # 不列入加減碼／新建倉統計，改在下方單獨顯示曝險。
    futures = [h for h in holdings if h.get("isFutures")]
    holdings = [h for h in holdings if not h.get("isFutures")]
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
    ]
    if futures:
        fl = "、".join(f"{h['code']} {h['name']} {h['shares']} 口（{h['todayWeight']}%）" for h in futures)
        lines.insert(4, f"⚡ 期貨部位：{fl}")
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
        for h in increased[:10]:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased[:10]:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


def main():
    log.info(f"=== {ETF_CODE} Check & Update started ===")

    raw_data, manager = fetch_fund_assets()
    if not raw_data:
        log.error("No data captured from Allianz API. Exiting.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 無法取得持股資料\n🔄 請檢查 Playwright 抓取是否正常")
        return
    if manager:
        log.info(f"Manager from API: {manager}")

    today_holdings, aum_ntd, units_zhang, nav, data_date_str = parse_fund_assets(raw_data)

    if not today_holdings:
        log.error("No holdings parsed. Exiting.")
        return

    run_date = datetime.now(timezone(timedelta(hours=8))).date()

    if not data_date_str:
        data_date_str = prev_trading_day(run_date).strftime("%Y-%m-%d")
        log.warning(f"NavDate missing, using prev trading day: {data_date_str}")
    else:
        # Validate: reject future dates and weekend dates (e.g. PCFDate is often next week's basket)
        try:
            parsed_date = datetime.strptime(data_date_str, "%Y-%m-%d").date()
            if parsed_date > run_date or parsed_date.weekday() >= 5 or parsed_date in TW_MARKET_HOLIDAYS:
                log.warning(f"API date {data_date_str} is future/weekend/holiday, clamping to prev trading day")
                data_date_str = prev_trading_day(run_date).strftime("%Y-%m-%d")
        except ValueError:
            data_date_str = prev_trading_day(run_date).strftime("%Y-%m-%d")
            log.warning(f"Could not parse API date, using prev trading day: {data_date_str}")

    log.info(f"Data date: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)
    log.info(f"Saved holdings snapshot: {json_path}")

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str,
                                  aum_ntd, units_zhang, manager=manager)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
