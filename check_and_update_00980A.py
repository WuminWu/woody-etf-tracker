"""
00980A ETF Holdings Daily Checker & Updater (野村智慧優選)

Logic:
1. Call POST /API/ETFAPI/api/Fund/GetFundAssets with SearchDate=prev_trading_day
2. Parse holdings JSON directly (no Excel/Playwright needed)
3. Compare with previous day's holdings
4. Fetch stock prices via yfinance
5. Generate data_00980A.json
6. Push to GitHub
7. Send Telegram notification
"""

import json
import os
import sys
import glob
import subprocess
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import yfinance as yf
from sheets_helper import append_holdings_to_sheets

# --------------- Config ---------------
API_URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00980A.json"
ETF_CODE = "00980A"
MANAGER = "游景德"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00980A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- Taiwan Market Holidays ---------------
# Update this set annually. Weekends are already skipped by weekday() check.
# Source: https://www.twse.com.tw/ (TWSE official holiday calendar)
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1),   # 元旦 New Year's Day
    date(2026, 2, 16),  # 農曆除夕 (Chinese New Year Eve)
    date(2026, 2, 17),  # 農曆初一 (Chinese New Year)
    date(2026, 2, 18),  # 農曆初二
    date(2026, 2, 19),  # 農曆初三
    date(2026, 2, 20),  # 農曆初四
    date(2026, 2, 28),  # 和平紀念日 (Peace Memorial Day)
    date(2026, 5, 1),   # 勞動節 (Labor Day)
    date(2026, 10, 10), # 國慶日 (National Day)
}

# --------------- Helpers ---------------

def prev_trading_day(date):
    """Return the previous trading day (skip weekends and Taiwan market holidays)."""
    d = date - timedelta(days=1)
    while d.weekday() >= 5 or d in TW_MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


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
        nav_date = table.get("NavDate", "")
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


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=0, units=0):
    prev_dict = {h["code"]: h for h in prev_holdings}
    # 讀取前一次 data JSON 取得各股前一交易日股價
    prev_prices_map = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _pf:
                _prev_json = json.load(_pf)
            for _ph in _prev_json.get("holdings", []):
                if _ph.get("price", 0) > 0:
                    prev_prices_map[_ph["code"]] = _ph["price"]
        except Exception:
            pass

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
            "prevPrice": prev_prices_map.get(h["code"], 0),
            "yestWeight": prev_data.get("weight", 0.0), "todayWeight": h["weight"],
            "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
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
                "prevPrice": prev_prices_map.get(prev_h["code"], 0),
                "yestWeight": prev_h["weight"], "todayWeight": 0.0,
                "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

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


    # AUM from official Nomura API; fallback to previous values if unavailable
    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_raw = units if units > 0 else (round(aum_ntd / etf_price) if aum_ntd > 0 and etf_price > 0 else 0)
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
                if _candidate.weekday() < 5 and _candidate not in TW_MARKET_HOLIDAYS:
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
    total_shares_zhang = total_shares_raw // 1000
    # AUM 合理性驗證：若新值與前一交易日相差超過 50%，視為資料來源解析異常，捨棄新值
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.1 or ratio > 5.0:
            log.warning(f"AUM 異常：totalShares={total_shares_zhang} 與前一交易日 {prev_total_shares} 相差 {ratio:.1%}，視為解析異常，改用前一交易日數值")
            total_shares_zhang = prev_total_shares
            total_market_cap = prev_total_market_cap
    # Fallback: if official source unavailable, keep previous values
    if total_shares_zhang == 0 and prev_total_shares > 0:
        total_shares_zhang = prev_total_shares
        total_market_cap = round(etf_price * prev_total_shares * 1000 / 1e8, 2) if etf_price > 0 else prev_total_market_cap
    wrapper = {
        "meta": {
            "manager": MANAGER, "ytd": ytd_val, "etfPrice": etf_price, "priceChange": price_change, "prevPrice": prev_price,
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
    log.info(f"{DATA_FILE} updated with {len(final_output)} holdings")
    return wrapper


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set.")
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
        f"📊 00980A 野村智慧優選 持股更新",
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


def git_push():
    try:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m", f"Auto-update 00980A holdings {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}"], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Git push completed.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")


# --------------- Main ---------------

def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    run_date_str = run_date.strftime("%Y-%m-%d")
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")

    log.info(f"=== 00980A Check & Update started ===")
    log.info(f"  Run date:  {run_date_str}")
    log.info(f"  Prev date: {data_date_str}")

    # Nomura API returns NavDate = actual data date.
    # Query today first; the API may already have today's holdings.
    # Use NavDate from response as the authoritative data date.
    today_holdings, aum_ntd, units, nav_date = fetch_holdings(run_date_str)
    actual_date_str = nav_date if nav_date else run_date_str

    if not today_holdings or not nav_date:
        # API not yet updated for today; fall back to yesterday
        log.warning("Today's Nomura API data not available. Falling back to previous trading day.")
        if holdings_exist_for(data_date_str):
            log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
            return
        today_holdings, aum_ntd, units, nav_date = fetch_holdings(data_date_str)
        actual_date_str = data_date_str
        if not today_holdings:
            log.error("No holdings fetched. Will retry next hour.")
            send_telegram(f"⏳ 00980A 野村智慧優選 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
            return
    else:
        log.info(f"NavDate from API: {actual_date_str}")
        if holdings_exist_for(actual_date_str):
            log.info(f"Holdings for {actual_date_str} already exist. Nothing to do.")
            return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{actual_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=actual_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, actual_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
