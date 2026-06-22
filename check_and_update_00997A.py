"""
00997A ETF Holdings Daily Checker & Updater (群益美國增長主動式ETF)

資料來源類型 D（capitalfund，Playwright + Angular）+ 海外股處理：
  https://www.capitalfund.com.tw/etf/product/detail/502/portfolio
  - 與 00992A/00982A 同平台：設日期 → 下載 Excel
  - 「參股」分頁欄位：股票代號 / 股票名稱 / 投資權重(%) / 股數
  - 持股為美股為主的全球股，代號為「TICKER 市場」格式（MU US、4062 JP、009150 KS、
    2330 為台股無後綴、BESI NA…）→ 價格用 00988A 式 market_map 對應 yfinance
  - 「投資組合」分頁含基金淨資產價值 / 已發行受益權單位總數

注意：此為海外/台美混合 ETF，daily_digest.py 已歸入 OVERSEAS_ETFS 群組（與 00988A 同一份報告）。
2026/4/14 掛牌（IPO 價 10），update_prices.py IPO_BASELINE 已設定。
"""

import json
import os
import sys
import glob
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

# --------------- Config ---------------
FUND_URL = "https://www.capitalfund.com.tw/etf/product/detail/502/portfolio"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00997A.json"
ETF_CODE = "00997A"
ETF_NAME = "群益美國增長"
MANAGER = "吳承恕"
IPO_DATE = "2026-04-14"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00997A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)

TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 28), date(2026, 5, 1),
    date(2026, 10, 10),
}


def next_trading_day(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5 or d in TW_MARKET_HOLIDAYS:
        d += timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def download_xlsx(date_str):
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
        page.evaluate(f"""
            var input = document.getElementById('condition-date');
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, '{date_str}');
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
        """)
        time.sleep(1)
        log.info(f"Date input set to: {date_input.input_value()}")

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
    """參股分頁（index 1）：代號(0)/名稱(1)/權重(2)/股數(3)。代號可為美股 TICKER（無數字）。"""
    df = pd.read_excel(xlsx_path, sheet_name=1, header=0)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        weight_str = str(row.iloc[2]).strip().replace("%", "")
        shares_str = str(row.iloc[3]).strip().replace(",", "")
        if not code or code in ("nan", "股票代號"):
            continue
        try:
            shares = int(float(shares_str))
            weight = float(weight_str)
        except ValueError:
            continue
        holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
    return holdings


def parse_aum_from_xlsx(xlsx_path):
    """投資組合分頁（index 0）：基金淨資產價值 / 已發行受益權單位總數。"""
    try:
        df = pd.read_excel(xlsx_path, sheet_name=0, header=None)
        aum_ntd, units = 0, 0
        for i in range(len(df)):
            label = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
            value = str(df.iloc[i, 1]).strip() if pd.notna(df.iloc[i, 1]) else ""
            val_clean = value.replace("TWD", "").replace(",", "").strip()
            if "淨資產價值" in label and "單位" not in label and val_clean:
                aum_ntd = int(float(val_clean))
            elif "已發行受益權單位總數" in label and val_clean:
                units = int(float(val_clean))
        log.info(f"AUM from XLSX: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM parse failed: {e}")
        return 0, 0


def get_previous_holdings(exclude_date_str):
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    prev_files = [f for f in sorted(glob.glob(pattern))
                  if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        log.info(f"Previous holdings: {os.path.basename(prev_files[-1])}")
        with open(prev_files[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("No previous holdings file found.")
    return []


# 海外股：代號為「TICKER 市場」。市場碼 → yfinance 後綴（同 00988A，新增 NA→.AS）與計價幣別。
MARKET_MAP = {
    "US": "", "JP": ".T", "KS": ".KS", "HK": ".HK", "GY": ".DE",
    "FP": ".PA", "LN": ".L", "SG": ".SI", "NA": ".AS",
}
MARKET_CCY = {
    "US": "USD", "JP": "JPY", "KS": "KRW", "HK": "HKD", "GY": "EUR",
    "FP": "EUR", "NA": "EUR", "LN": "GBP", "SG": "SGD",
}
_FX_CACHE = {}


def _fx_to_twd(ccy):
    """1 單位外幣 = ? 台幣（yfinance {CCY}TWD=X）。抓不到回傳 0 → 該股金額視為 0，不污染統計。"""
    if ccy == "TWD":
        return 1.0
    if ccy in _FX_CACHE:
        return _FX_CACHE[ccy]
    rate = 0.0
    try:
        hist = yf.Ticker(f"{ccy}TWD=X").history(period="5d", timeout=10)
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1])
    except Exception:
        pass
    _FX_CACHE[ccy] = rate
    return rate


def get_price(code_str):
    """回傳該股最新收盤價，**統一換算為新台幣**（diffAmount 才能跨幣別加總）。"""
    parts = code_str.strip().split()
    base = parts[0]
    if len(parts) == 1:   # 台股，本來就是台幣
        for suffix in (".TW", ".TWO"):
            try:
                hist = yf.Ticker(f"{base}{suffix}").history(period="1d", timeout=10)
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return 0.0
    market = parts[1].upper()
    yf_ticker = f"{base}{MARKET_MAP.get(market, '')}"
    try:
        hist = yf.Ticker(yf_ticker).history(period="1d", timeout=10)
        if hist.empty:
            return 0.0
        local_price = float(hist["Close"].iloc[-1])
    except Exception:
        return 0.0
    fx = _fx_to_twd(MARKET_CCY.get(market, "USD"))
    return round(local_price * fx, 2) if fx > 0 else 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=0, units=0):
    prev_dict = {h["code"]: h for h in prev_holdings}
    prev_prices_map = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _pf:
                for _ph in json.load(_pf).get("holdings", []):
                    if _ph.get("price", 0) > 0:
                        prev_prices_map[_ph["code"]] = _ph["price"]
        except Exception:
            pass

    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} holdings...")
    for i, h in enumerate(today_holdings):
        prev_data = prev_dict.get(h["code"], {})
        shares_prev = prev_data.get("shares", 0)
        diff_shares = h["shares"] - shares_prev
        price = get_price(h["code"])
        final_output.append({
            "code": h["code"], "name": h["name"],
            "shares": h["shares"], "prevShares": shares_prev,
            "price": round(price, 2), "prevPrice": prev_prices_map.get(h["code"], 0),
            "yestWeight": prev_data.get("weight", 0.0), "todayWeight": h["weight"],
            "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            price = get_price(prev_h["code"])
            final_output.append({
                "code": prev_h["code"], "name": prev_h["name"],
                "shares": 0, "prevShares": prev_h["shares"],
                "price": round(price, 2), "prevPrice": prev_prices_map.get(prev_h["code"], 0),
                "yestWeight": prev_h["weight"], "todayWeight": 0.0,
                "diffShares": -prev_h["shares"], "diffAmount": round(-prev_h["shares"] * price, 2),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    # ETF price & YTD（掛牌當年以 IPO 價為基準）
    ipo_year = int(IPO_DATE.split("-")[0])
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        if len(hist) >= 1:
            etf_price = round(float(hist["Close"].iloc[-1]), 2)
            if len(hist) >= 2:
                price_change = round(float((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100), 2)
                prev_price = round(float(hist["Close"].iloc[-2]), 2)
            if datetime.now(timezone(timedelta(hours=8))).year == ipo_year:
                ytd_val = f"{((etf_price - IPO_PRICE) / IPO_PRICE) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD (IPO baseline {IPO_PRICE}): {ytd_val}%")
            elif len(hist) >= 2:
                ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"ETF price fetch failed: {e}")

    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_zhang = (units // 1000) if units > 0 else 0
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                prev_meta = json.load(_f).get("meta", {})
            _d = datetime.strptime(data_date_str, "%Y-%m-%d").date()
            _delta = 1
            while True:
                _c = _d - timedelta(days=_delta)
                if _c.weekday() < 5 and _c not in TW_MARKET_HOLIDAYS:
                    _prev_td = _c.strftime("%Y-%m-%d"); break
                _delta += 1
            if prev_meta.get("dataDate", "") == _prev_td:
                prev_total_shares = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
        except Exception:
            pass
    if total_shares_zhang == 0 and prev_total_shares > 0:
        total_shares_zhang = prev_total_shares
        total_market_cap = round(etf_price * prev_total_shares * 1000 / 1e8, 2) if etf_price > 0 else prev_total_market_cap

    wrapper = {
        "meta": {
            "manager": MANAGER, "ytd": ytd_val, "etfPrice": etf_price,
            "priceChange": price_change, "prevPrice": prev_price,
            "dataDate": data_date_str,
            "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
            "totalShares": total_shares_zhang, "prevTotalShares": prev_total_shares,
            "totalMarketCap": total_market_cap, "prevTotalMarketCap": prev_total_market_cap,
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
            data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            if json.loads(r.read()).get("ok"):
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


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    run_date = now.date()
    data_date_str = run_date.strftime("%Y-%m-%d")
    form_date_str = next_trading_day(run_date).strftime("%Y/%m/%d")   # 輸入 T+1 取得 T 資料

    log.info(f"=== {ETF_CODE} Check & Update started ===")
    log.info(f"  Run/Data date: {data_date_str}　Form date: {form_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    xlsx_path = download_xlsx(form_date_str)
    if xlsx_path is None:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    aum_ntd, units = parse_aum_from_xlsx(xlsx_path)
    if not today_holdings:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return
    log.info(f"Parsed {len(today_holdings)} holdings for {data_date_str}")

    os.rename(xlsx_path, os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx"))
    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
