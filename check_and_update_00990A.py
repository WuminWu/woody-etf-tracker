"""
00990A ETF Holdings Daily Checker & Updater (元大全球AI新經濟主動式ETF)

資料來源類型 E（元大投信 yuantaetfs.com，Nuxt SSR）＋ 海外股處理：
  https://www.yuantaetfs.com/product/detail/00990A/ratio
  - 頁面為 Nuxt，持股與基金摘要完整內嵌於 window.__NUXT__（SSR）。
  - 用 Playwright 載入後讀 window.__NUXT__（瀏覽器已把 minified 變數參照解析成乾淨物件），
    再抽出：
      * FundWeights.StockWeights[]：{code:"LITE US"/"3037"（台股無後綴）, name, weights, qty}
      * weightData.PCF：{trandate:"YYYYMMDD"(揭露日), totalav(基金淨資產 NTD),
                          osunit(在外流通單位), nav}
  - 持股含美股(US)/日(JP)/韓(KP)/德(GR)/台股(無後綴)。海外股金額統一換算台幣，
    以便 diffAmount 跨幣別加總（同 00988A/00997A）。
  - 台股部位（代號無市場後綴，如 2330）會由 daily_digest 折入「台股報告」的個股統計。

注意：此為海外/台美混合 ETF。daily_digest.py 已將 00990A 歸入 OVERSEAS_ETFS 群組
      （與 00988A/00997A 同一份「海外/混合」報告，並自動延伸到週報）。
2025/12/22 掛牌（IPO 價 10）；因掛牌於 2025，2026 年 YTD 走年初基準（yfinance），不需 IPO_BASELINE。
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

import yfinance as yf
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets

# --------------- Config ---------------
FUND_URL = "https://www.yuantaetfs.com/product/detail/00990A/ratio"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00990A.json"
ETF_CODE = "00990A"
ETF_NAME = "元大全球AI新經濟"
MANAGER = "元大投信"
IPO_DATE = "2025-12-22"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00990A.log", encoding="utf-8"),
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


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


# 從 window.__NUXT__ 抽出「持股陣列」與「PCF 基金摘要」。以字串平衡括號解析，
# 並在字串內（引號中）時忽略括號，避免成分名稱含括號時誤判。
_EXTRACT_JS = r"""
() => {
  const s = JSON.stringify(window.__NUXT__);
  function matchBalanced(openIdx, open, close) {
    let depth = 0, inStr = false, esc = false;
    for (let k = openIdx; k < s.length; k++) {
      const c = s[k];
      if (inStr) {
        if (esc) { esc = false; }
        else if (c === '\\') { esc = true; }
        else if (c === '"') { inStr = false; }
        continue;
      }
      if (c === '"') { inStr = true; }
      else if (c === open) { depth++; }
      else if (c === close) { depth--; if (depth === 0) return k; }
    }
    return -1;
  }
  function extractArray(key) {
    const i = s.indexOf('"' + key + '":[');
    if (i < 0) return null;
    const start = s.indexOf('[', i);
    const end = matchBalanced(start, '[', ']');
    return end < 0 ? null : JSON.parse(s.slice(start, end + 1));
  }
  function extractObj(key) {
    const i = s.indexOf('"' + key + '":{');
    if (i < 0) return null;
    const start = s.indexOf('{', i);
    const end = matchBalanced(start, '{', '}');
    return end < 0 ? null : JSON.parse(s.slice(start, end + 1));
  }
  const holdings = extractArray('StockWeights');
  const pcf = extractObj('PCF');
  return { holdings, pcf };
}
"""


def fetch_from_nuxt():
    """回傳 (data_date_str, aum_ntd, units, holdings[list])；失敗回傳 (None, 0, 0, [])。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        log.info(f"Navigating to {FUND_URL} ...")
        page.goto(FUND_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_function(
                "() => window.__NUXT__ && JSON.stringify(window.__NUXT__).includes('StockWeights')",
                timeout=30000,
            )
        except Exception:
            log.error("window.__NUXT__ StockWeights 未就緒（頁面結構可能變動）。")
            browser.close()
            return None, 0, 0, []
        data = page.evaluate(_EXTRACT_JS)
        browser.close()

    holdings_raw = (data or {}).get("holdings") or []
    pcf = (data or {}).get("pcf") or {}
    trandate = str(pcf.get("trandate", "")).strip()      # e.g. "20260814"
    if len(trandate) == 8 and trandate.isdigit():
        data_date_str = f"{trandate[:4]}-{trandate[4:6]}-{trandate[6:]}"
    else:
        data_date_str = None
    aum_ntd = int(pcf.get("totalav", 0) or 0)
    units = int(pcf.get("osunit", 0) or 0)

    holdings = []
    for h in holdings_raw:
        code = str(h.get("code", "")).strip()
        name = str(h.get("name") or h.get("ename") or "").strip()
        try:
            qty = int(float(h.get("qty", 0)))
            weight = float(h.get("weights", 0))
        except (TypeError, ValueError):
            continue
        if not code or qty <= 0:
            continue
        holdings.append({"code": code, "name": name, "shares": qty, "weight": weight})
    return data_date_str, aum_ntd, units, holdings


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


# 海外股：代號為「TICKER 市場」。市場碼 → yfinance 後綴與計價幣別。
# 元大用碼：US 美股、JP 日、KP 韓（KOSPI）、GR 德。併入 00988A/00997A 既有對應以防未來擴充。
MARKET_MAP = {
    "US": "", "JP": ".T", "KP": ".KS", "KS": ".KS", "KQ": ".KQ", "GR": ".DE",
    "GY": ".DE", "HK": ".HK", "FP": ".PA", "LN": ".L", "SG": ".SI", "NA": ".AS",
}
MARKET_CCY = {
    "US": "USD", "JP": "JPY", "KP": "KRW", "KS": "KRW", "KQ": "KRW",
    "GR": "EUR", "GY": "EUR", "HK": "HKD", "FP": "EUR", "NA": "EUR",
    "LN": "GBP", "SG": "SGD",
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

    # ETF price & YTD（掛牌當年以 IPO 價為基準；跨年後用 yfinance 年初基準）
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
    log.info(f"=== {ETF_CODE} Check & Update started. Today: {now.strftime('%Y-%m-%d')} ===")

    data_date_str, aum_ntd, units, today_holdings = fetch_from_nuxt()
    if not data_date_str or not today_holdings:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n🔄 將於 30 分鐘後再次檢查...")
        return

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    log.info(f"Parsed {len(today_holdings)} holdings for {data_date_str}; AUM={aum_ntd/1e8:.2f}億, units={units:,}")

    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
