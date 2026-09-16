"""
00409A ETF Holdings Daily Checker & Updater (主動復華全球50)

資料來源：復華投信 API（純 HTTP，與 00991A 同一支，內部代號 ETF26）
  https://www.fhtrust.com.tw/api/assetsExcel/ETF26/{YYYYMMDD}
  - 版面：前段為「基金資產淨值 / 基金在外流通單位數」（標籤與數值上下兩列），
    接著「證券代號 | 證券名稱 | 股數 | 金額 | 權重(%)」表頭與持股。
  - 持股為全球股：代號「TICKER 市場」（US / KS / JP / CH…），台股無後綴。
    注意：不可沿用 00991A 的解析（它只收含數字的代號，會把 PLTR US、NVDA US 全部漏掉）。
  - 海外股價經 market_utils 換算為新台幣，diffAmount 才能跨幣別加總。

T+1：美股收盤後才公布，當日 18:00~21:00 只拿得到「前一個交易日」的檔
（與 00988A 同一套語意）。歸入海外/混合組。
2026/9/2 掛牌（IPO 價 10），掛牌當年 YTD 以 IPO 價為基準。
"""

import io
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

import pandas as pd
import yfinance as yf
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段
from market_utils import yf_symbol, ccy_of
from asset_allocation import format_scale_line

# --------------- Config ---------------
API_BASE = "https://www.fhtrust.com.tw/api/assetsExcel/ETF26"
HOLDINGS_DIR = "holdings"
ETF_CODE = "00409A"
ETF_NAME = "主動復華全球50"
DATA_FILE = f"data_{ETF_CODE}.json"
MANAGER = "胡家菱"
IPO_DATE = "2026-09-02"
IPO_PRICE = 10.0

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

# 2026 平日休市日（與 run_update.ps1 一致）
from tw_calendar import TW_MARKET_HOLIDAYS   # 單一來源：台股休市日（tw_calendar.py）


# --------------- Helpers ---------------

def prev_trading_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5 or d in TW_MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def download_xlsx(date_str):
    """下載指定資料日的 xlsx；該日尚未公布時 API 回傳非 xlsx 短內容 → 回傳 None。"""
    url = f"{API_BASE}/{date_str.replace('-', '')}"
    log.info(f"Downloading {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None
    if raw[:2] != b"PK":
        log.info(f"{date_str} 尚未公布（回應 {len(raw)} bytes，非 xlsx）")
        return None
    return raw


def parse_xlsx(raw):
    """回傳 (檔內資料日 YYYY-MM-DD, 淨資產 NTD, 在外流通單位數, holdings[])。"""
    df = pd.read_excel(io.BytesIO(raw), header=None, dtype=str)
    col0 = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[:, 0]]

    file_date, aum_ntd, units, header_idx = None, 0, 0, None
    for i, cell in enumerate(col0):
        nxt = re.sub(r"[^\d.]", "", col0[i + 1]) if i + 1 < len(col0) else ""
        if cell.startswith("日期"):
            m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", cell)
            if m:
                file_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        elif cell == "基金資產淨值" and nxt:
            aum_ntd = int(float(nxt))
        elif "在外流通單位數" in cell and nxt:
            units = int(float(nxt))
        elif cell == "證券代號":
            header_idx = i
            break

    holdings = []
    if header_idx is not None:
        for i in range(header_idx + 1, len(df)):
            code = col0[i]
            if not code or not re.match(r"^[0-9A-Za-z]", code):
                continue
            try:
                shares = int(float(str(df.iloc[i, 2]).replace(",", "").strip()))
                weight = float(str(df.iloc[i, 4]).replace("%", "").strip())
            except (TypeError, ValueError):
                continue
            holdings.append({"code": code, "name": str(df.iloc[i, 1]).strip(), "shares": shares, "weight": weight})

    log.info(f"檔內資料日 {file_date}；AUM {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億)；Units {units:,}；持股 {len(holdings)} 檔")
    return file_date, aum_ntd, units, holdings


def get_previous_holdings(exclude_date_str):
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    prev_files = [f for f in sorted(glob.glob(pattern))
                  if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        log.info(f"Previous holdings file: {os.path.basename(prev_files[-1])}")
        with open(prev_files[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("No previous holdings file found.")
    return []


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
        hist = hist[hist["Close"].notna()] if not hist.empty else hist   # 去掉未收盤的 NaN 列
        closes = hist["Close"].dropna() if not hist.empty else []
        if len(closes):
            rate = float(closes.iloc[-1])
    except Exception:
        pass
    _FX_CACHE[ccy] = rate
    return rate


def get_price(code_str):
    """回傳最新收盤價，**統一換算為新台幣**。"""
    parts = code_str.strip().split()
    base = parts[0]
    if len(parts) == 1:   # 台股
        for suffix in (".TW", ".TWO"):
            try:
                hist = yf.Ticker(f"{base}{suffix}").history(period="1d", timeout=10)
                hist = hist[hist["Close"].notna()] if not hist.empty else hist   # 去掉未收盤的 NaN 列
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return 0.0
    market = parts[1].upper()
    try:
        hist = yf.Ticker(yf_symbol(base, market)).history(period="1d", timeout=10)
        hist = hist[hist["Close"].notna()] if not hist.empty else hist   # 去掉未收盤的 NaN 列
        if hist.empty:
            return 0.0
        local_price = float(hist["Close"].iloc[-1])
    except Exception:
        return 0.0
    fx = _fx_to_twd(ccy_of(market))
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

    # ETF 股價與 YTD（掛牌當年以 IPO 價為基準；跨年後用年初第一個收盤價）
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        hist = hist[hist["Close"].notna()] if not hist.empty else hist   # 去掉未收盤的 NaN 列
        if len(hist) >= 2:
            last, prev = float(hist["Close"].iloc[-1]), float(hist["Close"].iloc[-2])
            base = IPO_PRICE if datetime.now(timezone(timedelta(hours=8))).year == int(IPO_DATE[:4]) \
                else float(hist["Close"].iloc[0])
            ytd_val = f"{(last - base) / base * 100:.2f}"
            etf_price = round(last, 2)
            prev_price = round(prev, 2)
            price_change = round((last - prev) / prev * 100, 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}% (base {base})")
    except Exception as e:
        log.warning(f"Failed to fetch ETF price/YTD: {e}")

    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_zhang = units // 1000 if units > 0 else 0
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                prev_meta = json.load(_f).get("meta", {})
            _ptd = prev_trading_day(datetime.strptime(data_date_str, "%Y-%m-%d").date()).strftime("%Y-%m-%d")
            if prev_meta.get("dataDate", "") == _ptd:
                prev_total_shares = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
            else:
                log.info(f"規模比較跳過：JSON dataDate={prev_meta.get('dataDate')} 非前一交易日({_ptd})")
        except Exception:
            pass
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.1 or ratio > 5.0:
            log.warning(f"規模異常：totalShares={total_shares_zhang} vs 前一交易日 {prev_total_shares}，改用前一交易日數值")
            total_shares_zhang, total_market_cap = prev_total_shares, prev_total_market_cap
    if total_shares_zhang == 0 and prev_total_shares > 0:
        total_shares_zhang = prev_total_shares
        total_market_cap = round(etf_price * prev_total_shares * 1000 / 1e8, 2) if etf_price > 0 else prev_total_market_cap

    wrapper = {
        "meta": {
            "manager": MANAGER, "ytd": ytd_val,
            "etfPrice": etf_price, "priceChange": price_change, "prevPrice": prev_price,
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


def fmt_zhang(shares):
    zhang = shares / 1000
    sign = "+" if zhang > 0 else ""
    return f"{sign}{int(zhang):,}張" if zhang == int(zhang) else f"{sign}{zhang:,.1f}張"


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0],
                       key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0],
                       key=lambda x: x["diffShares"])
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
    _scale = format_scale_line(meta)
    if _scale:
        lines[4:4] = _scale   # 插在「持股數量」與空行之間：基金規模（淨申購/淨贖回）
    if added:
        lines.append("\n✨ 新增持股：")
        for h in added:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['shares'])}（0% → {h['todayWeight']}%）")
    if removed:
        lines.append("\n🚫 出清持股：")
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
    lines.append("🔗 https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


# --------------- Main ---------------

def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    data_date_str = prev_trading_day(run_date).strftime("%Y-%m-%d")   # T+1
    log.info(f"=== {ETF_CODE} Check & Update started. Run {run_date}, target data date {data_date_str} ===")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    raw = download_xlsx(data_date_str)
    if raw is None:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    file_date, aum_ntd, units, today_holdings = parse_xlsx(raw)
    if file_date != data_date_str:
        log.warning(f"檔內日期 {file_date} 與目標 {data_date_str} 不符，視為尚未更新。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return
    if not today_holdings:
        log.error("解析不到任何持股，版面可能變動。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx"), "wb") as f:
        f.write(raw)
    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
