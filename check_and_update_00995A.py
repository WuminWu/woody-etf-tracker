"""
00995A ETF Holdings Daily Checker & Updater (主動中信台灣卓越)

Data source: MoneyDJ Basic0007B page
Columns: 個股名稱(含代碼), 投資比例(%), 持有股數
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
from html.parser import HTMLParser

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import yfinance as yf

# --------------- Config ---------------
ETF_CODE = "00995A"
ETF_NAME = "主動中信台灣卓越"
MANAGER = "TBD"
MONEYDJ_URL = f"https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={ETF_CODE}.TW"
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


# --------------- HTML Parser ---------------

class MoneyDJParser(HTMLParser):
    """Parse MoneyDJ Basic0007B holdings table.

    Column order: 個股名稱(code embedded), 投資比例(%), 持有股數
    Stock name format: "欣興(3037.TW)"
    """

    def __init__(self):
        super().__init__()
        self.holdings = []
        self._in_tr = False
        self._in_td = False
        self._cells = []
        self._current = ""
        self._page_text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._in_tr = True
            self._cells = []
        if self._in_tr and tag in ("td", "th"):
            self._in_td = True
            self._current = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_td:
            self._cells.append(self._current.strip())
            self._in_td = False
        if tag == "tr" and self._in_tr:
            self._process_row(self._cells)
            self._in_tr = False

    def handle_data(self, data):
        self._page_text += data
        if self._in_td:
            self._current += data

    def _process_row(self, cells):
        if len(cells) < 3:
            return
        name_col = cells[0].strip()
        # Extract stock code from "(3037.TW)" pattern
        m = re.search(r'\((\d{4,6}[A-Z0-9]*)\.TW[O]?\)', name_col)
        if not m:
            return
        code = m.group(1)
        name = name_col[:name_col.rfind("(")].strip()
        try:
            weight = float(cells[1].replace("%", "").replace(",", "").strip())
            shares = int(float(cells[2].replace(",", "").strip()))
        except (ValueError, IndexError):
            return
        if not re.fullmatch(r'\d{4,6}', code) or weight <= 0:
            return
        self.holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})


# --------------- Helpers ---------------

def prev_trading_day(date):
    d = date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def fetch_holdings():
    log.info(f"Fetching {MONEYDJ_URL} ...")
    try:
        req = urllib.request.Request(
            MONEYDJ_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-TW,zh;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
        parser = MoneyDJParser()
        parser.feed(html)
        holdings = parser.holdings
        log.info(f"Parsed {len(holdings)} holdings from MoneyDJ")
        return holdings
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None


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


def generate_data_json(today_holdings, prev_holdings, data_date_str):
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
                "yestWeight": prev_h["weight"], "todayWeight": 0.0,
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


    # Fetch ETF total assets (AUM) to derive 總股數 & 市值
    total_shares_raw, prev_total_shares, prev_total_market_cap = 0, 0, 0.0
    total_market_cap = 0.0
    try:
        _info = yf.Ticker(f"{ETF_CODE}.TW").info
        _assets = float(_info.get("totalAssets") or 0)
        if _assets > 0 and etf_price > 0:
            total_shares_raw = round(_assets / etf_price)
            total_market_cap = round(_assets / 1e8, 2)
    except Exception:
        pass
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            prev_total_shares = _prev.get("meta", {}).get("totalShares", 0)
            prev_total_market_cap = _prev.get("meta", {}).get("totalMarketCap", 0.0)
        except Exception:
            pass
    total_shares_zhang = total_shares_raw // 1000
    wrapper = {
        "meta": {
            "manager": MANAGER, "ytd": ytd_val, "etfPrice": etf_price,
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
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']}")
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
    run_date = datetime.now().date()
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")

    log.info(f"=== {ETF_CODE} Check & Update started ===")
    log.info(f"  Run date:  {run_date}")
    log.info(f"  Data date: {data_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    today_holdings = fetch_holdings()
    if not today_holdings:
        log.error("No holdings fetched. Will retry next hour.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於下一個小時再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str)

    git_push()
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
