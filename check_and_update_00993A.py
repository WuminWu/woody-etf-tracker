"""
00993A ETF Holdings Daily Checker & Updater (主動安聯台灣)

Data source: Allianz official site - intercepts GetFundAssets API via Playwright
https://etf.allianzgi.com.tw/etf-info/E0002?tab=4
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
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets

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
        pcf_date_str = str(fa.get("PCFDate", fa.get("NavDate", ""))).replace("/", "-")
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


def generate_data_json(today_holdings, prev_holdings, data_date_str,
                       aum_ntd, units_zhang, manager=None):
    prev_dict = {h["code"]: h for h in prev_holdings}
    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} holdings...")

    for i, h in enumerate(today_holdings):
        prev_data = prev_dict.get(h["code"], {})
        shares_prev = prev_data.get("shares", 0)
        diff_shares = h["shares"] - shares_prev

        if h.get("is_futures"):
            price = 0.0
        else:
            price = get_price(h["code"])

        final_output.append({
            "code": h["code"], "name": h["name"],
            "shares": h["shares"], "prevShares": shares_prev,
            "price": round(price, 2),
            "yestWeight": prev_data.get("weight", 0.0),
            "todayWeight": h["weight"],
            "diffShares": diff_shares,
            "diffAmount": round(diff_shares * price, 2),
            "isFutures": h.get("is_futures", False),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    # Stocks that were removed (present in prev but not today)
    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            price = 0.0 if prev_h.get("isFutures") else get_price(prev_h["code"])
            diff_shares = -prev_h["shares"]
            final_output.append({
                "code": prev_h["code"], "name": prev_h["name"],
                "shares": 0, "prevShares": prev_h["shares"],
                "price": round(price, 2),
                "yestWeight": prev_h.get("weight", 0.0), "todayWeight": 0.0,
                "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
                "isFutures": prev_h.get("isFutures", False),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    # ETF price and YTD from yfinance
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        if len(hist) >= 2:
            ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
            etf_price = round(float(hist["Close"].iloc[-1]), 2)
            price_change = round(float((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100), 2)
            prev_price = round(float(hist["Close"].iloc[-2]), 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"ETF price fetch failed: {e}")

    # Use Allianz API data for totalShares and totalMarketCap if available
    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_zhang = units_zhang if units_zhang > 0 else 0

    # Fallback: derive from yfinance totalAssets
    if total_shares_zhang == 0:
        try:
            _info = yf.Ticker(f"{ETF_CODE}.TW").info
            _assets = float(_info.get("totalAssets") or 0)
            if _assets > 0 and etf_price > 0:
                total_shares_zhang = round(_assets / etf_price) // 1000
                total_market_cap = round(_assets / 1e8, 2)
        except Exception:
            pass

    # Load previous totalShares/totalMarketCap
    # prevTotalShares：只在前一個交易日才做比較，避免腳本跳日造成跨多天誤差
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            prev_meta = _prev.get("meta", {})
            # 計算前一個交易日（跳過週末）
            _d = datetime.strptime(data_date_str, "%Y-%m-%d").date()
            _delta = 1
            while True:
                _candidate = _d - timedelta(days=_delta)
                if _candidate.weekday() < 5:
                    _prev_trading_day = _candidate.strftime("%Y-%m-%d")
                    break
                _delta += 1
            if prev_meta.get("dataDate", "") == _prev_trading_day:
                prev_total_shares = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
            else:
                log.info(f"AUM 比較跳過：JSON dataDate={prev_meta.get('dataDate')} 非前一交易日({_prev_trading_day})")
        except Exception:
            pass
    # AUM 合理性驗證：若新值與前一交易日相差超過 50%，視為資料來源解析異常，捨棄新值
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.5 or ratio > 2.0:
            log.warning(f"AUM 異常：totalShares={total_shares_zhang} 與前一交易日 {prev_total_shares} 相差 {ratio:.1%}，視為解析異常，改用前一交易日數值")
            total_shares_zhang = prev_total_shares
            total_market_cap = prev_total_market_cap

    wrapper = {
        "meta": {
            "manager": manager or MANAGER, "ytd": ytd_val, "etfPrice": etf_price, "priceChange": price_change, "prevPrice": prev_price,
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
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['todayWeight']}%）")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased[:10]:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['todayWeight']}%）")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/woody-etf-tracker/")
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

    if not data_date_str:
        data_date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        log.warning(f"PCFDate missing, using today: {data_date_str}")

    log.info(f"Data date: {data_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)
    log.info(f"Saved holdings snapshot: {json_path}")

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str,
                                  aum_ntd, units_zhang, manager=manager)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
