"""
00995A ETF Holdings Daily Checker & Updater (主動中信台灣卓越)

Data source: CTBC Investments official API
https://www.ctbcinvestments.com/Etf/00653201/Combination
API: https://www.ctbcinvestments.com.tw/API/etf/ETFHoldingWeight
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段

# --------------- Config ---------------
ETF_CODE = "00995A"
ETF_NAME = "主動中信台灣卓越"
MANAGER = "中信投信"
CTBC_FID = "E0036"
CTBC_BASE = "https://www.ctbcinvestments.com.tw/API"
CTBC_REFERER = "https://www.ctbcinvestments.com/"
HOLDINGS_DIR = "holdings"
DATA_FILE = f"data_{ETF_CODE}.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

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


# --------------- CTBC API ---------------

def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Referer": CTBC_REFERER},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def get_ctbc_token():
    resp = _post(
        f"{CTBC_BASE}/home/AuthToken?token=www.ctbcinvestments.com",
        {"token": "www.ctbcinvestments.com"}
    )
    token = resp["Data"]["token"]
    log.info(f"CTBC token acquired: {token[:30]}...")
    return token


def fetch_manager(token):
    """Fetch fund manager name from CTBC ETFDetail API."""
    try:
        encoded_token = urllib.parse.quote(token, safe="")
        resp = _post(
            f"{CTBC_BASE}/etf/ETFDetail?token={encoded_token}",
            {"token": token, "CNO": "00653201"}
        )
        details = resp.get("Data", {}).get("FundDetail", [])
        if details:
            manager = details[0].get("Manager", "")
            if manager:
                log.info(f"Manager from API: {manager}")
                return manager
    except Exception as e:
        log.warning(f"Failed to fetch manager: {e}")
    return None


def fetch_holdings_for_date(token, date_str):
    """Fetch holdings data for a given date (format: YYYY/MM/DD)."""
    encoded_token = urllib.parse.quote(token, safe="")
    resp = _post(
        f"{CTBC_BASE}/etf/ETFHoldingWeight?token={encoded_token}",
        {"token": token, "FID": CTBC_FID, "StartDate": date_str}
    )
    if resp.get("ResultCode") != 0:
        log.error(f"API error for {date_str}: {resp.get('ResultMsg')}")
        return None
    return resp["Data"]


def parse_holdings_data(data):
    """
    Parse CTBC ETFHoldingWeight response.
    Returns (holdings_list, aum_ntd, units_zhang, nav, data_date_str)
    """
    fa = data["FundAssets"][0]

    # NAV_DT is reliable for date
    nav_dt = fa.get("NAV_DT", "")[:10]  # "2026-04-21"
    data_date_str = nav_dt if nav_dt else ""

    # AUM and units: find numeric string values by descending size
    # The two largest numbers are AUM (billions) and units (hundred millions)
    aum_ntd, units_raw = 0, 0
    numeric_vals = []
    for v in fa.values():
        if isinstance(v, str) and re.match(r'^\d{1,3}(,\d{3})+$', v):
            numeric_vals.append(int(v.replace(",", "")))
    numeric_vals.sort(reverse=True)
    if len(numeric_vals) >= 1:
        aum_ntd = numeric_vals[0]
    if len(numeric_vals) >= 2:
        units_raw = numeric_vals[1]
    units_zhang = units_raw // 1000

    # NAV: find the decimal float value
    nav = 0.0
    for v in fa.values():
        if isinstance(v, str) and re.match(r'^\d+\.\d+$', v):
            try:
                nav = float(v)
                break
            except Exception:
                pass

    log.info(f"AUM: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units_zhang:,}張, NAV: {nav}, Date: {data_date_str}")

    # Parse stock holdings from FundAssetsDetail where Code == "STOCK"
    holdings = []
    fad = data.get("FundAssetsDetail", [])
    stock_section = next((x for x in fad if x.get("Code") == "STOCK"), None)
    if stock_section:
        for item in stock_section.get("Data", []):
            code = str(item.get("code_", "")).strip()
            name = str(item.get("name_", "")).strip()
            qty_str = str(item.get("qty_", "0")).replace(",", "")
            weight_str = str(item.get("weights_", "0"))
            try:
                shares = int(float(qty_str))
                weight = float(weight_str)
            except Exception:
                continue
            if not re.match(r'^\d{4,6}$', code) or weight <= 0:
                continue
            holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})

    log.info(f"Parsed {len(holdings)} stock holdings")
    return holdings, aum_ntd, units_zhang, nav, data_date_str


# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00995A", name="主動中信台灣卓越", manager="中信投信",
                 units_are_zhang=True, aum_yf_fallback=True, aum_carry_prev=False)


# --------------- Helpers ---------------

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

    token = get_ctbc_token()
    manager = fetch_manager(token)

    # Fetch today's data
    today_tw = datetime.now(timezone(timedelta(hours=8)))
    today_date_str = today_tw.strftime("%Y/%m/%d")
    log.info(f"Fetching data for {today_date_str}...")

    raw_data = fetch_holdings_for_date(token, today_date_str)
    if not raw_data:
        log.error("Failed to fetch today's data. Exiting.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 無法取得持股資料")
        return

    today_holdings, aum_ntd, units_zhang, nav, data_date_str = parse_holdings_data(raw_data)
    if not today_holdings:
        log.error("No holdings parsed. Exiting.")
        return

    if not data_date_str:
        data_date_str = today_tw.strftime("%Y-%m-%d")

    log.info(f"Data date from API: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    # Bootstrap: if no previous holdings file exists, fetch previous trading day from CTBC
    prev_date = prev_trading_day(datetime.strptime(data_date_str, "%Y-%m-%d").date())
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    if not holdings_exist_for(CFG, prev_date_str):
        log.info(f"No previous holdings found. Bootstrapping {prev_date_str} from CTBC...")
        prev_api_date = prev_date.strftime("%Y/%m/%d")
        prev_raw = fetch_holdings_for_date(token, prev_api_date)
        if prev_raw:
            prev_h_list, _, _, _, _ = parse_holdings_data(prev_raw)
            if prev_h_list:
                prev_json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{prev_date_str}.json")
                with open(prev_json_path, "w", encoding="utf-8") as f:
                    json.dump(prev_h_list, f, ensure_ascii=False, indent=2)
                log.info(f"Bootstrapped previous holdings: {prev_json_path}")

    # Save today's snapshot
    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)
    log.info(f"Saved holdings snapshot: {json_path}")

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd, units_zhang, manager=manager)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
