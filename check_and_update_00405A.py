"""
00405A ETF Holdings Daily Checker & Updater (主動富邦台灣龍耀)

資料來源類型 K：富邦投信 ETF 投資網「基金資產」頁（伺服器端渲染 HTML，純 HTTP 即可）
  https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=00405A
  - 持股表欄位：股票代碼 / 名稱 / 股數 / 金額 / 權重(%)
  - 頁面標示「資料日期：YYYY/MM/DD」「基金淨資產(新台幣)」「基金在外流通單位數(單位)」
  - 以「資料日期 == 今天」驗證，避免假日 / 官網未更新時寫入舊資料

注意：00405A 於 2026/6/9 掛牌（IPO 價 10 元），update_prices.py 的 IPO_BASELINE
已設定掛牌年以 IPO 價計算 YTD。
"""

import json
import os
import sys
import re
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
HOLDINGS_URL = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=00405A"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00405A.json"
ETF_CODE = "00405A"
ETF_NAME = "主動富邦台灣龍耀"
MANAGER = "高晧欣"
IPO_DATE = "2026-06-09"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00405A.log", encoding="utf-8"),
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


# --------------- Fetch & parse ---------------

def _fetch_html():
    req = urllib.request.Request(
        HOLDINGS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _text_lines(html):
    """去標籤後的純文字行（label 與值常相鄰兩行）。"""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL)
    t = re.sub(r"<[^>]+>", "\n", t)
    return [l.strip() for l in t.split("\n") if l.strip()]


def _value_after(lines, label):
    for i, l in enumerate(lines):
        if label in l:
            # 同行帶值（label：value）或下一行
            m = re.search(re.escape(label) + r"[：:]\s*([\d,/\.]+)", l)
            if m:
                return m.group(1)
            for nxt in lines[i + 1:i + 3]:
                m2 = re.fullmatch(r"[\d,\.]+", nxt)
                if m2:
                    return nxt
    return None


def fetch_data():
    """回傳 (holdings, data_date_str, aum_ntd, units) 或 (None, ...) 失敗。"""
    try:
        html = _fetch_html()
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None, None, 0, 0

    # 持股表
    holdings = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
        if len(cells) >= 5 and re.fullmatch(r"\d{4,6}", cells[0]):
            try:
                holdings.append({
                    "code": cells[0],
                    "name": cells[1],
                    "shares": int(cells[2].replace(",", "")),
                    "weight": float(cells[4].replace("%", "").replace(",", "")),
                })
            except ValueError:
                continue

    lines = _text_lines(html)
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{2}/\d{2})", html)
    data_date_str = m.group(1).replace("/", "-") if m else None

    aum_str = _value_after(lines, "基金淨資產(新台幣)")
    units_str = _value_after(lines, "基金在外流通單位數(單位)")
    aum_ntd = int(aum_str.replace(",", "")) if aum_str else 0
    units = int(units_str.replace(",", "")) if units_str else 0

    log.info(f"Parsed {len(holdings)} holdings, date={data_date_str}, "
             f"AUM={aum_ntd:,} ({aum_ntd/1e8:.2f}億), units={units:,}")
    return holdings, data_date_str, aum_ntd, units


# --------------- Shared helpers (同其他台股腳本) ---------------

def get_previous_holdings(exclude_date_str):
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    prev_files = [f for f in sorted(glob.glob(pattern)) if exclude_date_str not in os.path.basename(f)]
    if prev_files:
        log.info(f"Previous holdings: {os.path.basename(prev_files[-1])}")
        with open(prev_files[-1], "r", encoding="utf-8") as f:
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
                for _ph in json.load(_pf).get("holdings", []):
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

    # 與前一交易日比較（AUM 合理性 / fallback）
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
                    _prev_trading_day = _c.strftime("%Y-%m-%d")
                    break
                _delta += 1
            if prev_meta.get("dataDate", "") == _prev_trading_day:
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


# --------------- Main ---------------

def main():
    run_date_str = datetime.now(timezone(timedelta(hours=8))).date().strftime("%Y-%m-%d")
    log.info(f"=== {ETF_CODE} Check & Update started ===")
    log.info(f"  Run date: {run_date_str}")

    holdings, data_date_str, aum_ntd, units = fetch_data()
    if not holdings:
        log.error("No holdings fetched. Will retry next run.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {run_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 資料日期驗證：官網日期須等於今天，否則視為尚未更新
    if data_date_str != run_date_str:
        log.info(f"官網資料日期 {data_date_str} != 今天 {run_date_str}，尚未更新。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新（官網仍為 {data_date_str}）\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    if os.path.exists(json_path):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
