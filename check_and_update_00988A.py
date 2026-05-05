"""
00988A ETF Holdings Daily Checker & Updater

Logic:
1. Download the holdings XLSX from ezmoney.com.tw
2. Check if the date in the Excel matches today
3. If YES → save it, compare with previous day's holdings, generate data_00988A.json, push to GitHub
4. If NO → exit (Task Scheduler will retry next hour)
5. If today's file already exists → skip entirely (already done for today)
"""

import json
import os
import sys
import glob
import subprocess
import time
import logging
from datetime import date, datetime, timedelta, timezone

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
import yfinance as yf
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets

# --------------- Taiwan Market Holidays 2026 ---------------
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1),   # 元旦
    date(2026, 2, 16),  # 農曆除夕
    date(2026, 2, 17),  # 農曆初一
    date(2026, 2, 18),  # 農曆初二
    date(2026, 2, 19),  # 農曆初三
    date(2026, 2, 20),  # 農曆初四
    date(2026, 2, 28),  # 和平紀念日
    date(2026, 5, 1),   # 勞動節
    date(2026, 10, 10), # 國慶日
}

# --------------- Config ---------------
FUND_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=61YTW"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00988A.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- Helpers ---------------

def minguo_to_date(minguo_str):
    """Convert Minguo date string like '115/04/17' to datetime.date"""
    parts = minguo_str.strip().split("/")
    year = int(parts[0]) + 1911
    month = int(parts[1])
    day = int(parts[2])
    return datetime(year, month, day).date()


def get_prev_trading_day():
    """Return the previous trading day in Taiwan time (skip weekends and Taiwan market holidays)."""
    tw_now = datetime.now(timezone(timedelta(hours=8))).date()
    delta = 1
    while True:
        candidate = tw_now - timedelta(days=delta)
        if candidate.weekday() < 5 and candidate not in TW_MARKET_HOLIDAYS:
            return candidate
        delta += 1


def prev_holdings_exist():
    """Check if we already have the previous trading day's holdings file."""
    prev_str = get_prev_trading_day().strftime("%Y-%m-%d")
    filepath = os.path.join(HOLDINGS_DIR, f"00988A_holdings_{prev_str}.json")
    return os.path.exists(filepath)


def download_xlsx():
    """Download the XLSX from ezmoney and return (temp_path, date_in_file)."""
    tmp_path = os.path.join(HOLDINGS_DIR, "_temp_download.xlsx")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        log.info(f"Navigating to {FUND_URL} ...")
        page.goto(FUND_URL, wait_until="networkidle")
        time.sleep(3)

        # Click 基金投資組合 tab
        portfolio_link = page.locator("a:has-text('基金投資組合')")
        if portfolio_link.count() > 0:
            portfolio_link.first.click()
            log.info("Clicked 基金投資組合 tab")
            page.wait_for_timeout(5000)
        else:
            log.warning("基金投資組合 tab not found, trying anchor link")
            page.goto(FUND_URL + "#asset", wait_until="networkidle")
            page.wait_for_timeout(5000)

        # Scroll down to find export button
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Find and click export button inside #asset tab
        export_btn = page.locator("#asset button:has-text('匯出XLSX檔')")
        if export_btn.count() == 0:
            export_btn = page.locator("#asset button:has-text('匯出')")
        if export_btn.count() == 0:
            browser.close()
            log.error("Cannot find export button!")
            return None, None

        with page.expect_download(timeout=30000) as download_info:
            export_btn.first.evaluate("el => el.click()")
            log.info("Evaluated click on export button, waiting for download...")

        download = download_info.value
        download.save_as(tmp_path)
        original_name = download.suggested_filename
        log.info(f"Downloaded: {original_name}")
        browser.close()

    # Parse date from the Excel header (first column name is like "資料日:115/04/17")
    df = pd.read_excel(tmp_path)
    header_col = df.columns[0]  # e.g. "資料日:115/04/17"
    log.info(f"Excel header column: {header_col}")

    # Extract Minguo date
    if ":" in header_col or "：" in header_col:
        date_part = header_col.replace("：", ":").split(":")[-1].strip()
    else:
        date_part = header_col.strip()

    try:
        file_date = minguo_to_date(date_part)
        log.info(f"Date in file: {file_date}")
    except Exception as e:
        log.error(f"Failed to parse date from '{date_part}': {e}")
        return tmp_path, None

    return tmp_path, file_date


def parse_holdings_from_xlsx(xlsx_path):
    """Parse the holdings Excel into a list of dicts."""
    df = pd.read_excel(xlsx_path)
    stock_data = []
    # Stock data starts around row 19 (0-indexed), with columns: code, name, shares, weight
    for idx in range(19, len(df)):
        row = df.iloc[idx]
        code = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        shares_str = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else "0"
        weight_str = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else "0%"

        if code and code != "nan" and len(code) >= 4:
            shares = int(shares_str.replace(",", ""))
            weight = float(weight_str.replace("%", "")) if "%" in weight_str else 0.0
            stock_data.append({
                "code": code,
                "name": name,
                "shares": shares,
                "weight": weight,
            })
    return stock_data


def parse_aum_from_xlsx(xlsx_path):
    """Parse AUM from ezmoney XLSX header rows (before holdings at row 19)."""
    try:
        df = pd.read_excel(xlsx_path)
        aum_ntd, units = 0, 0
        for i in range(min(15, len(df))):
            row = df.iloc[i]
            cell0 = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            cell1 = str(row.iloc[1]) if len(row) > 1 and pd.notna(row.iloc[1]) else ""
            if "淨資產" in cell0 and cell1:
                aum_str = str(cell1).replace("NTD", "").replace(",", "").strip()
                try:
                    aum_ntd = int(float(aum_str))
                except Exception:
                    pass
            elif "流通在外單位數" in cell0 and cell1:
                try:
                    units = int(float(str(cell1).replace(",", "").strip()))
                except Exception:
                    pass
        log.info(f"AUM from XLSX: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM parse from XLSX failed: {e}")
        return 0, 0


def get_previous_holdings():
    """Find the most recent previous holdings JSON file and load it."""
    pattern = os.path.join(HOLDINGS_DIR, "00988A_holdings_*.json")
    files = sorted(glob.glob(pattern))

    prev_str = get_prev_trading_day().strftime("%Y-%m-%d")
    # Filter out prev trading day's file (the one we just saved) and temporary files
    prev_files = [
        f for f in files
        if prev_str not in os.path.basename(f) and "_temp" not in f
    ]

    if prev_files:
        latest = prev_files[-1]
        log.info(f"Previous holdings file: {os.path.basename(latest)}")
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        log.warning("No previous holdings file found.")
        return []


def get_price(code_str):
    """Fetch current stock price from Yahoo Finance."""
    parts = code_str.strip().split()
    base = parts[0]
    
    if len(parts) == 1:
        # Taiwan stock
        suffixes = [".TW", ".TWO"]
        for suffix in suffixes:
            try:
                hist = yf.Ticker(f"{base}{suffix}").history(period="1d", timeout=10)
                if not hist.empty: return float(hist["Close"].iloc[-1])
            except: pass
    elif len(parts) == 2:
        market = parts[1].upper()
        # Mapping for international markets
        market_map = {
            "US": "", "JP": ".T", "KS": ".KS", "HK": ".HK", 
            "GY": ".DE", "FP": ".PA", "LN": ".L", "SG": ".SI"
        }
        yf_ticker = f"{base}{market_map.get(market, '')}"
        try:
            hist = yf.Ticker(yf_ticker).history(period="1d", timeout=10)
            if not hist.empty: return float(hist["Close"].iloc[-1])
        except: pass
        
    return 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=0, units=0):
    """Compare today vs previous holdings, fetch prices, generate data_00988A.json."""
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
        code = h["code"]
        name = h["name"]
        shares_today = h["shares"]
        weight_today = h["weight"]

        prev_data = prev_dict.get(code, {})
        shares_prev = prev_data.get("shares", 0)
        weight_prev = prev_data.get("weight", 0.0)

        diff_shares = shares_today - shares_prev
        price = get_price(code)
        diff_amount = diff_shares * price

        final_output.append({
            "code": code,
            "name": name,
            "shares": shares_today,
            "prevShares": shares_prev,
            "price": round(price, 2),
            "prevPrice": prev_prices_map.get(h["code"], 0),
            "yestWeight": weight_prev,
            "todayWeight": weight_today,
            "diffShares": diff_shares,
            "diffAmount": round(diff_amount, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    # Sort by todayWeight descending
    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)

    # Assign ranks
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    # Also include stocks that existed in prev but no longer in today (removed)
    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            code = prev_h["code"]
            price = get_price(code)
            diff_shares = -prev_h["shares"]
            final_output.append({
                "code": code,
                "name": prev_h["name"],
                "shares": 0,
                "prevShares": prev_h["shares"],
                "price": round(price, 2),
                "prevPrice": prev_prices_map.get(prev_h["code"], 0),
                "yestWeight": prev_h["weight"],
                "todayWeight": 0.0,
                "diffShares": diff_shares,
                "diffAmount": round(diff_shares * price, 2),
                "rank": len(final_output) + 1,
            })

    # Calculate YTD & ETF Price
    ytd_val = "0.0"
    etf_price = 0.0
    price_change = 0.0
    prev_price = 0.0
    try:
        etf_ticker = yf.Ticker("00988A.TW")
        ytd_hist = etf_ticker.history(period="ytd", timeout=10)
        if len(ytd_hist) >= 2:
            first_price = ytd_hist["Close"].iloc[0]
            last_price = ytd_hist["Close"].iloc[-1]
            ytd_calc = ((last_price - first_price) / first_price) * 100
            ytd_val = f"{ytd_calc:.2f}"
            etf_price = round(float(last_price), 2)
            price_change = round(float((last_price - ytd_hist["Close"].iloc[-2]) / ytd_hist["Close"].iloc[-2] * 100), 2)
            prev_price = round(float(ytd_hist["Close"].iloc[-2]), 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"Failed to fetch ETF price/YTD: {e}")


    # AUM from official ezmoney XLSX; fallback to previous values if unavailable
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
    # AUM 合理性驗證：若新值與前一交易日相差超過 50%，視為 XLSX 解析異常，捨棄新值
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
            "manager": "陳意婷",
            "ytd": ytd_val,
            "etfPrice": etf_price, "priceChange": price_change, "prevPrice": prev_price,
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

    log.info(f"data_00988A.json updated with {len(final_output)} holdings")
    return wrapper


def send_telegram(message):
    """Send a Telegram message via Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set. Skipping notification.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram notification sent.")
            else:
                log.warning(f"Telegram API error: {result}")
    except Exception as e:
        log.warning(f"Failed to send Telegram notification: {e}")


def fmt_zhang(shares):
    """Format shares as 張 (1張=1000股), with sign."""
    zhang = shares / 1000
    sign = "+" if zhang > 0 else ""
    if zhang == int(zhang):
        return f"{sign}{int(zhang):,}張"
    return f"{sign}{zhang:,.1f}張"


def build_notification(wrapper, etf_code="00988A", etf_name="主動統一全球創新"):
    """Build a summary notification message from the data wrapper."""
    meta = wrapper["meta"]
    holdings = wrapper["holdings"]

    added    = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed  = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted(
        [h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0],
        key=lambda x: x["diffShares"], reverse=True
    )
    decreased = sorted(
        [h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0],
        key=lambda x: x["diffShares"]
    )

    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 {etf_code} {etf_name} 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
    ]

    if added:
        lines.append("\n✨ 新增持股：")
        for h in added:
            zhang = fmt_zhang(h["shares"])
            lines.append(f"  • {h['code']} {h['name']}　{zhang}（0% → {h['todayWeight']}%）")

    if removed:
        lines.append("\n🚫 出清持股：")
        for h in removed:
            zhang = fmt_zhang(-h.get("prevShares", 0))
            lines.append(f"  • {h['code']} {h['name']}　{zhang}")

    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")

    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['yestWeight']}% → {h['todayWeight']}%）")

    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("🔗 https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


def git_push():
    """Commit and push changes to GitHub."""
    try:
        subprocess.run(["git", "add", "-A"], check=True)
        msg = f"Auto-update holdings {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", msg], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Git push completed successfully.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")


# --------------- Main ---------------

def main():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    today_str = today.strftime("%Y-%m-%d")
    prev_trading_day = get_prev_trading_day()
    prev_str = prev_trading_day.strftime("%Y-%m-%d")
    log.info(f"=== Check & Update started. Today: {today_str}, checking for: {prev_str} ===")

    # 1. Skip if previous trading day already done
    if prev_holdings_exist():
        log.info(f"Holdings for {prev_str} already downloaded. Nothing to do.")
        return

    # 2. Download XLSX and check date
    xlsx_path, file_date = download_xlsx()

    if xlsx_path is None:
        log.error("Download failed. Will retry.")
        send_telegram(f"⏳ 00988A 主動統一全球創新 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date is None:
        log.error("Could not parse date from file. Will retry.")
        send_telegram(f"⏳ 00988A 主動統一全球創新 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 00988A 含海外（美股）成分，ezmoney 可能以台灣時間編製日期標記 XLSX（比實際交易日多1天）。
    # 因此同時接受 file_date == prev_trading_day（正常）及 file_date 超前1~2天（全球ETF慣例）。
    # 無論哪種情況，一律以 prev_str 作為 dataDate，與統一官方網站標示一致。
    date_delta = (file_date - prev_trading_day).days
    if date_delta < 0 or date_delta > 2:
        log.info(f"File date ({file_date}) not compatible with prev trading day ({prev_trading_day}) "
                 f"(delta={date_delta} days). Not yet updated.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ 00988A 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if date_delta > 0:
        log.info(f"File date ({file_date}) is {date_delta} day(s) ahead of prev trading day ({prev_trading_day}). "
                 f"Using {prev_str} as canonical dataDate (aligns with official source).")

    # 3. Date matches (or is within tolerance)! Save and process
    log.info(f"File date acceptable for prev trading day ({prev_str})! Processing...")

    # Save XLSX with proper name
    final_xlsx = os.path.join(HOLDINGS_DIR, f"00988A_holdings_{prev_str}.xlsx")
    os.rename(xlsx_path, final_xlsx)

    # Parse today's holdings
    today_holdings = parse_holdings_from_xlsx(final_xlsx)
    log.info(f"Parsed {len(today_holdings)} stocks from today's holdings")
    aum_ntd, units = parse_aum_from_xlsx(final_xlsx)

    # Save as JSON
    json_path = os.path.join(HOLDINGS_DIR, f"00988A_holdings_{prev_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    # 4. Load previous day's holdings and generate diff
    prev_holdings = get_previous_holdings()
    wrapper = generate_data_json(today_holdings, prev_holdings, prev_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets("00988A", wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    # 5. Send Telegram notification (git push handled by GitHub Actions workflow)
    msg = build_notification(wrapper, etf_code="00988A", etf_name="主動統一全球創新")
    send_telegram(msg)

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
