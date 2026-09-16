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
import logging
from datetime import date, datetime, timedelta, timezone

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段
from market_utils import yf_symbol, ccy_of

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

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00990A", name="主動元大全球AI新經濟", manager="元大投信",
                 ipo_date="2025-12-22", ipo_price=10.0)


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

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    log.info(f"Parsed {len(today_holdings)} holdings for {data_date_str}; AUM={aum_ntd/1e8:.2f}億, units={units:,}")

    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
