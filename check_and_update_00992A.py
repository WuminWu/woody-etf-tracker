"""
00992A ETF Holdings Daily Checker & Updater (群益科技創新)

Logic:
1. Navigate to capitalfund.com.tw/etf/product/detail/500/portfolio
2. Set date to today and download Excel
3. Parse holdings from Excel (sheet: 參股)
4. Compare with previous day's holdings
5. Fetch stock prices via yfinance
6. Generate data_00992A.json
7. Push to GitHub
"""

import json
import os
import sys
import glob
import subprocess
import time
import logging
from datetime import datetime, timedelta

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
import yfinance as yf
from playwright.sync_api import sync_playwright

# --------------- Config ---------------
FUND_URL = "https://www.capitalfund.com.tw/etf/product/detail/500/portfolio"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00992A.json"
ETF_CODE = "00992A"
MANAGER = "葉薏婷"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00992A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- Helpers ---------------

def next_trading_day(date):
    """Return the next calendar day, skipping weekends (not holidays).
    Capital Fund website: selecting date D gives the holdings of the previous trading day.
    So to fetch today's holdings, we must input tomorrow's date (next trading day).
    """
    d = date + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def prev_trading_day(date):
    """Return the previous trading day (skip weekends)."""
    d = date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    filepath = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json")
    return os.path.exists(filepath)


def download_xlsx(date_str):
    """Download holdings Excel from Capital Fund website for the given date (yyyy/mm/dd)."""
    tmp_path = os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        log.info(f"Navigating to {FUND_URL} ...")
        page.goto(FUND_URL, wait_until="networkidle", timeout=30000)
        time.sleep(3)

        date_input = page.locator("#condition-date")
        if not date_input.is_visible():
            log.error("Date input not found!")
            browser.close()
            return None

        # Angular date picker requires JS to set value and dispatch events correctly.
        # direct .type() fills each segment incorrectly due to structured input format.
        page.evaluate(f"""
            var input = document.getElementById('condition-date');
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, '{date_str}');
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
        """)
        time.sleep(1)
        actual = date_input.input_value()
        log.info(f"Date input set to: {actual}")

        btn = page.locator("button.buyback-search-section-btn")
        if btn.count() == 0:
            log.error("Download button not found!")
            browser.close()
            return None

        log.info(f"Clicking download button for date {date_str}...")
        with page.expect_download(timeout=30000) as dl_info:
            btn.first.click()

        dl = dl_info.value
        dl.save_as(tmp_path)
        log.info(f"Downloaded: {dl.suggested_filename}")
        browser.close()

    return tmp_path


def parse_holdings_from_xlsx(xlsx_path):
    """Parse holdings from the Capital Fund Excel file (sheet index 1 = 參股)."""
    df = pd.read_excel(xlsx_path, sheet_name=1, header=0)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        weight_str = str(row.iloc[2]).strip().replace("%", "")
        shares_str = str(row.iloc[3]).strip().replace(",", "")
        if code and code != "nan" and len(code) >= 4 and any(c.isdigit() for c in code):
            try:
                shares = int(float(shares_str))
                weight = float(weight_str)
                holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
            except Exception:
                pass
    return holdings


def get_previous_holdings(exclude_date_str):
    """Find the most recent holdings JSON excluding the given date."""
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    files = sorted(glob.glob(pattern))
    prev_files = [f for f in files if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        latest = prev_files[-1]
        log.info(f"Previous holdings file: {os.path.basename(latest)}")
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        log.warning("No previous holdings file found.")
        return []


def get_price(code):
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = yf.Ticker(f"{code}{suffix}")
            hist = ticker.history(period="1d", timeout=10)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str):
    prev_dict = {h["code"]: h for h in prev_holdings}
    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} stocks...")

    for i, h in enumerate(today_holdings):
        code = h["code"]
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
            "name": h["name"],
            "shares": shares_today,
            "prevShares": shares_prev,
            "price": round(price, 2),
            "yestWeight": weight_prev,
            "todayWeight": weight_today,
            "diffShares": diff_shares,
            "diffAmount": round(diff_amount, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    # Stocks that were removed (out cleared)
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
                "yestWeight": prev_h["weight"],
                "todayWeight": 0.0,
                "diffShares": diff_shares,
                "diffAmount": round(diff_shares * price, 2),
                "rank": len(final_output) + 1,
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    ytd_val = "0.00"
    etf_price = 0.0
    try:
        etf_ticker = yf.Ticker(f"{ETF_CODE}.TW")
        ytd_hist = etf_ticker.history(period="ytd", timeout=10)
        if len(ytd_hist) >= 2:
            first_price = ytd_hist["Close"].iloc[0]
            last_price = ytd_hist["Close"].iloc[-1]
            ytd_calc = ((last_price - first_price) / first_price) * 100
            ytd_val = f"{ytd_calc:.2f}"
            etf_price = round(float(last_price), 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"Failed to fetch ETF price/YTD: {e}")

    wrapper = {
        "meta": {
            "manager": MANAGER,
            "ytd": ytd_val,
            "etfPrice": etf_price,
            "dataDate": data_date_str,
            "lastUpdate": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "holdings": final_output,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=4)

    log.info(f"{DATA_FILE} updated with {len(final_output)} holdings")
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
            "parse_mode": "HTML",
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


def build_notification(wrapper, etf_code="00992A", etf_name="群益科技創新"):
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
        f"📊 <b>{etf_code} {etf_name} 持股更新</b>",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
    ]

    if added:
        lines.append("\n✨ <b>新增持股：</b>")
        for h in added:
            zhang = fmt_zhang(h["shares"])
            lines.append(f"  • {h['code']} {h['name']}　{zhang}（{h['todayWeight']}%）")

    if removed:
        lines.append("\n🚫 <b>出清持股：</b>")
        for h in removed:
            zhang = fmt_zhang(-h.get("prevShares", 0))
            lines.append(f"  • {h['code']} {h['name']}　{zhang}")

    if increased:
        lines.append("\n🔴 <b>加碼明細：</b>")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}")

    if decreased:
        lines.append("\n🟢 <b>減碼明細：</b>")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}")

    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']}")
    lines.append("🔗 https://wuminwu.github.io/etf-tracker/")
    return "\n".join(lines)


def git_push():
    try:
        subprocess.run(["git", "add", "-A"], check=True)
        msg = f"Auto-update 00992A holdings {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", msg], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Git push completed successfully.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")


# --------------- Main ---------------

def main():
    # Capital Fund date logic:
    #   Selecting date D on the website returns holdings from the PREVIOUS trading day.
    #   So to get today's (trading day T) holdings, we input the next trading day (T+1).
    #
    # Script runs on T+1 (the next trading day after market close of T).
    # - form_date  = today (T+1) → fetches T's holdings
    # - data_date  = prev_trading_day(today) = T
    # - save file  = data_date

    now = datetime.now()
    run_date = now.date()
    # The actual holdings date = the previous trading day relative to today
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")
    form_date_str = run_date.strftime("%Y/%m/%d")  # input into the website form

    log.info(f"=== 00992A Check & Update started ===")
    log.info(f"  Run date (today):    {run_date}")
    log.info(f"  Form date (website): {form_date_str}")
    log.info(f"  Data date (actual):  {data_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    xlsx_path = download_xlsx(form_date_str)
    if xlsx_path is None:
        log.error("Download failed. Will retry next hour.")
        return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    if not today_holdings:
        log.error("No holdings parsed from Excel. Will retry next hour.")
        return

    log.info(f"Parsed {len(today_holdings)} stocks for {data_date_str}")

    # Save with the ACTUAL data date
    final_xlsx = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx")
    os.rename(xlsx_path, final_xlsx)

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str)

    git_push()

    msg = build_notification(wrapper, etf_code="00992A", etf_name="群益科技創新")
    send_telegram(msg)

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
