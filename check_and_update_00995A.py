"""
00995A ETF Holdings Daily Checker & Updater (主動中信台灣卓越)

Data source: CTBC Investments official API
https://www.ctbcinvestments.com/Etf/00653201/Combination
API: https://www.ctbcinvestments.com.tw/API/etf/ETFHoldingWeight
"""

import glob
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import yfinance as yf

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


# --------------- Helpers ---------------

def prev_trading_day(date):
    d = date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def get_previous_holdings(exclude_date_str):
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    files = sorted(glob.glob(pattern))
    prev_files = [f for f in files if exclude_date_str not in os.path.basename(f)]
    if prev_files:
        latest = prev_files[-1]
        log.info(f"Previous holdings: {os.path.basename(latest)}")
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("No previous holdings file found.")
    return []


def get_price(code):
    for suffix in [".TW", ".TWO"]:
        try:
            hist = yf.Ticker(f"{code}{suffix}").history(period="1d", timeout=10)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd, units_zhang, manager=None):
    prev_dict = {h["code"]: h for h in prev_holdings}
    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} stocks...")

    for i, h in enumerate(today_holdings):
        prev_data = prev_dict.get(h["code"], {})
        shares_prev = prev_data.get("shares", 0)
        diff_shares = h["shares"] - shares_prev
        price = get_price(h["code"])
        final_output.append({
            "code": h["code"], "name": h["name"],
            "shares": h["shares"], "prevShares": shares_prev,
            "price": round(price, 2),
            "yestWeight": prev_data.get("weight", 0.0),
            "todayWeight": h["weight"],
            "diffShares": diff_shares,
            "diffAmount": round(diff_shares * price, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            price = get_price(prev_h["code"])
            diff_shares = -prev_h["shares"]
            final_output.append({
                "code": prev_h["code"], "name": prev_h["name"],
                "shares": 0, "prevShares": prev_h["shares"],
                "price": round(price, 2),
                "yestWeight": prev_h.get("weight", 0.0), "todayWeight": 0.0,
                "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    ytd_val, etf_price = "0.00", 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        if len(hist) >= 2:
            ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
            etf_price = round(float(hist["Close"].iloc[-1]), 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"ETF price fetch failed: {e}")

    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_zhang = units_zhang if units_zhang > 0 else 0

    # Fallback to yfinance totalAssets
    if total_shares_zhang == 0:
        try:
            _info = yf.Ticker(f"{ETF_CODE}.TW").info
            _assets = float(_info.get("totalAssets") or 0)
            if _assets > 0 and etf_price > 0:
                total_shares_zhang = round(_assets / etf_price) // 1000
                total_market_cap = round(_assets / 1e8, 2)
        except Exception:
            pass

    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            prev_total_shares = _prev.get("meta", {}).get("totalShares", 0)
            prev_total_market_cap = _prev.get("meta", {}).get("totalMarketCap", 0.0)
        except Exception:
            pass

    wrapper = {
        "meta": {
            "manager": manager or MANAGER, "ytd": ytd_val, "etfPrice": etf_price,
            "dataDate": data_date_str,
            "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
            "totalShares": total_shares_zhang,
            "prevTotalShares": prev_total_shares,
            "totalMarketCap": total_market_cap,
            "prevTotalMarketCap": prev_total_market_cap,
        },
        "holdings": final_output,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=4)
    log.info(f"{DATA_FILE} updated: {len(final_output)} holdings, {total_shares_zhang:,}張, {total_market_cap}億")
    return wrapper


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                log.info("Telegram notification sent.")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")


def fmt_zhang(shares):
    zhang = shares / 1000
    sign = "+" if zhang > 0 else ""
    return f"{sign}{int(zhang):,}張" if zhang == int(zhang) else f"{sign}{zhang:,.1f}張"


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
    ]
    if added:
        lines.append("\n新增持股：")
        for h in added:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['shares'])}（{h['todayWeight']}%）")
    if removed:
        lines.append("\n出清持股：")
        for h in removed:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(-h.get('prevShares', 0))}")
    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased[:10]:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased[:10]:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/etf-tracker/")
    return "\n".join(lines)


def git_push():
    try:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", f"Auto-update {ETF_CODE} holdings {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}"], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Git push completed.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")


# --------------- Main ---------------

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

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    # Bootstrap: if no previous holdings file exists, fetch previous trading day from CTBC
    prev_date = prev_trading_day(datetime.strptime(data_date_str, "%Y-%m-%d").date())
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    if not holdings_exist_for(prev_date_str):
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

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd, units_zhang, manager=manager)

    git_push()
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
