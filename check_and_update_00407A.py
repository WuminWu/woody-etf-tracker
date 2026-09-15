"""
00407A ETF Holdings Daily Checker & Updater (主動凱基台灣)

資料來源：凱基投信「申購買回清單」Excel（純 HTTP，不需 Playwright、不需登入）
  https://www.kgifund.com.tw/Fund/DownLoadRedemptionExcel?fundID=J024&queryDate=MM/DD/YYYY 00:00:00
  - fundID J024 = 主動凱基台灣（00407A）
  - queryDate 是「清單生效日」（下一個交易日），內容為前一個交易日（T）收盤後的持股。
    實際資料日一律以檔內「(YYYY/MM/DD)每受益權單位淨資產價值」標籤為準，不自行推算
    （避免假日表不完整時推錯日期）。
  - 分頁「申購買回清單公告」：基金淨資產價值、已發行受益權單位總數
  - 分頁「股票」：股票代號 / 股票名稱 / 股數 / 權重(%)

台股組、當日揭露（與 00981A 等相同）。2026/6/24 掛牌（IPO 價 10），掛牌當年 YTD 以 IPO 價為基準。
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
from asset_allocation import format_scale_line

# --------------- Config ---------------
FUND_ID = "J024"
API_URL = "https://www.kgifund.com.tw/Fund/DownLoadRedemptionExcel"
LIST_PAGE = "https://www.kgifund.com.tw/Fund/RedemptionList"
HOLDINGS_DIR = "holdings"
ETF_CODE = "00407A"
ETF_NAME = "主動凱基台灣"
DATA_FILE = f"data_{ETF_CODE}.json"
MANAGER = "趙偉志"
IPO_DATE = "2026-06-24"
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

# 2026 平日休市日（與 run_update.ps1 一致）。只用來推算 queryDate；資料日以檔內標籤為準。
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 2, 12), date(2026, 2, 13), date(2026, 2, 16),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 2, 27), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1),
    date(2026, 6, 19), date(2026, 7, 10), date(2026, 9, 25), date(2026, 9, 28),
    date(2026, 10, 9), date(2026, 10, 26), date(2026, 12, 25),
}


# --------------- Helpers ---------------

def is_trading_day(d):
    return d.weekday() < 5 and d not in TW_MARKET_HOLIDAYS


def next_trading_day(d):
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d):
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def download_xlsx(query_date):
    """下載 queryDate 的申購買回清單；回傳 xlsx bytes，失敗或非 xlsx 回傳 None。"""
    q = urllib.parse.quote(query_date.strftime("%m/%d/%Y") + " 00:00:00")
    url = f"{API_URL}?fundID={FUND_ID}&queryDate={q}"
    log.info(f"Downloading {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": LIST_PAGE})
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None
    if raw[:2] != b"PK":
        log.warning(f"Response is not an xlsx ({len(raw)} bytes)")
        return None
    return raw


def parse_summary(raw):
    """分頁「申購買回清單公告」→ (資料日 YYYY-MM-DD, 淨資產 NTD, 已發行單位數)。"""
    df = pd.read_excel(io.BytesIO(raw), sheet_name="申購買回清單公告", header=None)
    nav_date, aum_ntd, units = None, 0, 0
    for _, row in df.iterrows():
        label = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        value = str(row.iloc[1]).strip() if df.shape[1] > 1 and pd.notna(row.iloc[1]) else ""
        num = re.sub(r"[^\d.\-]", "", value)
        if "每受益權單位淨資產價值" in label:
            m = re.search(r"\((\d{4})/(\d{1,2})/(\d{1,2})\)", label)
            if m:
                nav_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        elif "基金淨資產價值" in label and num:
            aum_ntd = int(float(num))
        elif "已發行受益權單位總數" in label and num:
            units = int(float(num))
    log.info(f"資料日 {nav_date}；AUM {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億)；Units {units:,}")
    return nav_date, aum_ntd, units


def parse_holdings(raw):
    """分頁「股票」：股票代號 / 股票名稱 / 股數 / 權重(%)。以字串讀取保留代號前導零。"""
    df = pd.read_excel(io.BytesIO(raw), sheet_name="股票", header=0, dtype=str)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
            continue
        try:
            shares = int(float(str(row.iloc[2]).replace(",", "").strip()))
            weight = float(str(row.iloc[3]).replace("%", "").strip())
        except (TypeError, ValueError):
            continue
        holdings.append({"code": code, "name": str(row.iloc[1]).strip(), "shares": shares, "weight": weight})
    return holdings


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


def get_price(code):
    for suffix in (".TW", ".TWO"):
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

    # ETF 股價與 YTD（掛牌當年以 IPO 價為基準；跨年後用年初第一個收盤價）
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
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
    # 合理性驗證：與前一交易日相差 10 倍以上視為解析異常，改用前一交易日數值
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


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set. Skipping notification.")
        return
    try:
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                     data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if json.loads(resp.read()).get("ok"):
                log.info("Telegram notification sent.")
    except Exception as e:
        log.warning(f"Failed to send Telegram notification: {e}")


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
    query_date = next_trading_day(run_date)
    log.info(f"=== {ETF_CODE} Check & Update started. Run {run_date}, queryDate {query_date} ===")

    raw = download_xlsx(query_date)
    if raw is None:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{run_date}\n🔄 將於 30 分鐘後再次檢查...")
        return

    data_date_str, aum_ntd, units = parse_summary(raw)
    if not data_date_str:
        log.error("檔內找不到「(YYYY/MM/DD)每受益權單位淨資產價值」標籤，版面可能變動。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{run_date}\n🔄 將於 30 分鐘後再次檢查...")
        return
    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist（今日清單可能尚未公布）. Nothing to do.")
        return

    today_holdings = parse_holdings(raw)
    if not today_holdings:
        log.error("「股票」分頁解析不到任何持股。")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return
    log.info(f"Parsed {len(today_holdings)} stocks for {data_date_str}")

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
