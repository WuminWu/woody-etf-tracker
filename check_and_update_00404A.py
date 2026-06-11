"""
00404A ETF Holdings Daily Checker & Updater (主動聯博動能50 / 聯博台灣動能收益50主動式ETF)

Logic (類型 I：AllianceBernstein webapi JSON):
1. GET https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/investor/TW00000404A5/holdings
   - domesticHoldings[0] (holdings-section-equity) 為股票持股
   - holdingCode 為 ISIN（TW000 + 4碼股票代號 + 3碼），parse 出股票代號
   - holdingPerc 已是百分比、holdingShares 股數
   - 排除 options/futures section
2. GET .../basket → navAsOfDate, nav, aum, shares（在外流通單位數）
3. 驗證 asOfDate == 今天才寫入
4. 股票中文名稱：API 只回英文名，從其他 data_*.json 建立 代號→中文名 對照，
   缺漏時 fallback 用精簡英文名
5. 比對前一日、抓股價、產生 data_00404A.json、寫入 Sheets、發 Telegram

注意：00404A 於 2026/6/9 掛牌（IPO 價 10 元），update_prices.py 的 IPO_BASELINE
已設定掛牌年以 IPO 價計算 YTD。
"""

import json
import os
import re
import sys
import glob
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import yfinance as yf
from sheets_helper import append_holdings_to_sheets

# --------------- Config ---------------
API_BASE = "https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/investor/TW00000404A5"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00404A.json"
ETF_CODE = "00404A"
MANAGER = "聯博投信"
IPO_DATE = "2026-06-09"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00404A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)

# --------------- Taiwan Market Holidays ---------------
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 2, 28),
    date(2026, 5, 1),
    date(2026, 10, 10),
}

# 內建中文名稱表：API 只回英文名，其他 ETF 也沒持有的股票由此補上
# （查無對照時 fallback 為精簡英文名，發現新英文名可隨時補充此表）
STATIC_CN_NAMES = {
    "2357": "華碩", "6414": "樺漢", "5434": "崇越", "3265": "台星科",
    "4766": "南寶", "4961": "天鈺", "6679": "鈺太", "3034": "聯詠",
    "3010": "華立", "6640": "均華",
}

# 其他 ETF 的 data JSON，用來查股票中文名稱
OTHER_DATA_FILES = [
    "data_00981A.json", "data_00403A.json", "data_00980A.json", "data_00985A.json",
    "data_00991A.json", "data_00992A.json", "data_00982A.json", "data_00987A.json",
    "data_00993A.json", "data_00995A.json", "data_00996A.json",
]


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def api_get(path):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def build_cn_name_map():
    """從其他 ETF data JSON 收集 代號→中文名稱 對照。"""
    name_map = {}
    for f in OTHER_DATA_FILES:
        if not os.path.exists(f):
            continue
        try:
            d = json.loads(open(f, encoding="utf-8").read())
            for h in d.get("holdings", []):
                if h.get("code") and h.get("name"):
                    name_map.setdefault(h["code"], h["name"])
        except Exception:
            pass
    return name_map


def simplify_en_name(name):
    """精簡英文名稱：去掉 ORD TWD 10 / CORPORATION 等冗詞。"""
    n = name
    n = re.sub(r'\s+ORD\s+TWD\s*[\d.]+$', '', n)
    n = re.sub(r'\s+(CO LTD|CORPORATION|CORP|INC|LTD|COMPANY)\.?$', '', n)
    n = re.sub(r'\s+(CO LTD|CORPORATION|CORP|INC|LTD)\.?\s', ' ', n)
    return n.strip().title()


def parse_stock_code(isin):
    """台股 ISIN: TW000 + 4碼股票代號 + 3碼 → 回傳股票代號，無法解析回傳 None。"""
    m = re.fullmatch(r'TW000(\w{4})\d{3}', isin or "")
    return m.group(1) if m else None


def fetch_holdings_and_meta():
    """
    Returns (holdings, as_of_date_str, aum_ntd, units)
      holdings: [{code, name, shares, weight}, ...] 僅股票 section
    """
    data = api_get("/holdings")
    sections = data.get("domesticHoldings", [])
    equity = next((s for s in sections if s.get("holdingCategory") == "holdings-section-equity"), None)
    if not equity:
        return [], "", 0, 0

    # asOfDate 格式 "06/11/2026" → "2026-06-11"
    as_of = equity.get("asOfDate", "")
    as_of_str = ""
    m = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', as_of)
    if m:
        as_of_str = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"

    cn_names = build_cn_name_map()
    holdings = []
    for h in equity.get("holdings", []):
        code = parse_stock_code(h.get("holdingCode", ""))
        if not code:
            continue
        shares = int(h.get("holdingShares") or 0)
        if shares <= 0:
            continue
        weight = round(float(h.get("holdingPerc") or 0), 2)
        name = cn_names.get(code) or STATIC_CN_NAMES.get(code) or simplify_en_name(h.get("holding", code))
        holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})

    # basket: AUM 與在外流通單位數
    aum_ntd, units = 0, 0
    try:
        basket = api_get("/basket")
        aum_ntd = int(basket.get("aum") or 0)
        units = int(basket.get("shares") or 0)
    except Exception as e:
        log.warning(f"basket fetch failed: {e}")

    return holdings, as_of_str, aum_ntd, units


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

    # ETF price & YTD（掛牌當年以 IPO 價為 YTD 基準）
    ipo_year = int(IPO_DATE.split("-")[0])
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        if len(hist) >= 1:
            etf_price = round(float(hist["Close"].iloc[-1]), 2)
            if len(hist) >= 2:
                price_change = round(float((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100), 2)
                prev_price = round(float(hist["Close"].iloc[-2]), 2)
            current_year = datetime.now(timezone(timedelta(hours=8))).year
            if current_year == ipo_year:
                ytd_val = f"{((etf_price - IPO_PRICE) / IPO_PRICE) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD (IPO baseline {IPO_PRICE}): {ytd_val}%")
            elif len(hist) >= 2:
                ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"ETF price fetch failed: {e}")

    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_raw = units if units > 0 else (round(aum_ntd / etf_price) if aum_ntd > 0 and etf_price > 0 else 0)
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            prev_meta = _prev.get("meta", {})
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
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.1 or ratio > 5.0:
            log.warning(f"AUM 異常：totalShares={total_shares_zhang} 與前一交易日 {prev_total_shares} 相差 {ratio:.1%}，改用前一交易日數值")
            total_shares_zhang = prev_total_shares
            total_market_cap = prev_total_market_cap
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
        f"📊 00404A 主動聯博動能50 持股更新",
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


# --------------- Main ---------------

def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    data_date_str = run_date.strftime("%Y-%m-%d")

    log.info(f"=== 00404A Check & Update started ===")
    log.info(f"  Run date / Data date: {data_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    try:
        today_holdings, as_of_str, aum_ntd, units = fetch_holdings_and_meta()
    except Exception as e:
        log.error(f"API fetch failed: {e}")
        send_telegram(f"⏳ 00404A 主動聯博動能50 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    log.info(f"Parsed {len(today_holdings)} holdings, asOfDate: {as_of_str}, "
             f"AUM: {aum_ntd:,} NTD, Units: {units:,}")

    # 日期驗證：API asOfDate 必須等於今天（防官網未更新與排程跨日）
    if not today_holdings or as_of_str != data_date_str:
        log.info(f"asOfDate {as_of_str} != today {data_date_str} (or no holdings). Not updated yet.")
        send_telegram(f"⏳ 00404A 主動聯博動能50 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
