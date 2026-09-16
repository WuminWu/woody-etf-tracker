"""
00400A ETF Holdings Daily Checker & Updater (國泰台股動能高息主動式ETF)

Logic (類型 J：cathaysite cwapi JSON API):
1. GET https://cwapi.cathaysite.com.tw/api/ETF/GetETFDetailStockList?FundCode=EA&SearchDate=YYYY/MM/DD
   - 回傳 [{stockCode, stockName(中文), volumn("440,000"), weights("8.54")}, ...]
   - 查無該日資料時 returnCode=4005 → 視為尚未更新
2. GET .../api/ETF/GetETFAssets?fundCode=EA
   - preDate（資料日期）、fundNav（淨資產）、fundOutstandingShares（在外流通單位數）
3. 驗證 preDate == 今天才寫入
4. 比對前一日、抓股價、產生 data_00400A.json、寫入 Sheets、發 Telegram

注意：00400A 於 2026/4/9 掛牌（IPO 價 10 元），update_prices.py 的 IPO_BASELINE
已設定掛牌年以 IPO 價計算 YTD。配息為「月配」。
"""

import json
import os
import re
import sys
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段

# --------------- Config ---------------
API_BASE = "https://cwapi.cathaysite.com.tw/api"
FUND_CODE = "EA"            # cwapi 內部代碼（網址 slug 是 EEA，API 用 EA）
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00400A.json"
ETF_CODE = "00400A"
MANAGER = "梁恩溢"
IPO_DATE = "2026-04-09"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00400A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00400A", name="國泰動能高息", manager="梁恩溢",
                 ipo_date="2026-04-09", ipo_price=10.0)


# --------------- Helpers ---------------

def api_get(path):
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "Referer": "https://www.cathaysite.com.tw/"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_holdings(search_date_str):
    """
    search_date_str: "YYYY/MM/DD"
    Returns [{code, name, shares, weight}, ...]；該日無資料回傳 []。
    """
    qs = urllib.parse.urlencode({"FundCode": FUND_CODE, "SearchDate": search_date_str})
    d = api_get(f"ETF/GetETFDetailStockList?{qs}")
    if d.get("returnCode") != "2000" or not isinstance(d.get("result"), list):
        log.info(f"StockList returnCode={d.get('returnCode')} ({d.get('returnMessage')})")
        return []
    holdings = []
    for it in d["result"]:
        code = (it.get("stockCode") or "").strip()
        if not re.fullmatch(r'\d{4,6}[A-Z]?', code):
            continue
        try:
            shares = int((it.get("volumn") or "0").replace(",", ""))
            weight = float(it.get("weights") or 0)
        except ValueError:
            continue
        holdings.append({"code": code, "name": (it.get("stockName") or code).strip(),
                         "shares": shares, "weight": weight})
    return holdings


def fetch_assets():
    """Returns (data_date_str 'YYYY-MM-DD', aum_ntd, units)。"""
    d = api_get(f"ETF/GetETFAssets?fundCode={FUND_CODE}")
    res = d.get("result") or {}
    pre_date = (res.get("preDate") or "").replace("/", "-")
    aum = int((res.get("fundNav") or "0").replace(",", "") or 0)
    units = int((res.get("fundOutstandingShares") or "0").replace(",", "") or 0)
    return pre_date, aum, units


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: x["diffShares"])
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00400A 國泰台股動能高息 持股更新",
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
    search_date_str = run_date.strftime("%Y/%m/%d")

    log.info(f"=== 00400A Check & Update started ===")
    log.info(f"  Run date / Data date: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    try:
        today_holdings = fetch_holdings(search_date_str)
        assets_date, aum_ntd, units = fetch_assets()
    except Exception as e:
        log.error(f"API fetch failed: {e}")
        send_telegram(f"⏳ 00400A 國泰台股動能高息 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    log.info(f"Parsed {len(today_holdings)} holdings, assets date: {assets_date}, "
             f"AUM: {aum_ntd:,} NTD, Units: {units:,}")

    # 日期驗證：以今天為 SearchDate 查詢，查無資料即尚未更新；
    # GetETFAssets 的 preDate 也需等於今天（雙重驗證，防排程跨日）
    if not today_holdings or assets_date != data_date_str:
        log.info(f"Not updated yet (holdings={len(today_holdings)}, assetsDate={assets_date}).")
        send_telegram(f"⏳ 00400A 國泰台股動能高息 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
