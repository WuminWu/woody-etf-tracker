"""
00413A ETF Holdings Daily Checker & Updater (主動玉山關鍵科技)

資料來源：玉山投信 ETF 專區 API（純 HTTP，無需 Playwright）
  POST https://www.esunam.com/ETFAPI/GetFundAssets   body {"FundID": "54"}
  - FundID 是玉山內部基金編號（54），不是證券代號（傳 "00413A" 回「查無資料」）；
    由 POST /ETFAPI/GetETFOverview 查得（StcokNo=00413A → FundNo=54）
  - **API 不吃日期參數，永遠回最新一天**（傳 SearchDate 也被忽略）→ 資料日期一律以
    回傳的 FundAsset.NavDate 為準，不是交易日、晚於今天、或缺漏就不寫入
  - Entries.Data.FundAsset：Aum（淨資產元）、Units（流通單位數）、Nav、NavDate（YYYY/MM/DD）
  - Entries.Data.Table：第一張是資產配置（股票／期貨／現金），TableTitle="股票" 那張才是持股
    （欄位：股票代號、股票名稱、股數、權重(%)）

Logic:
1. 抓最新持股，驗證 NavDate
2. 該日 holdings 已存在 → 結束；否則存檔、與前一份比較
3. 產生 data_00413A.json、寫試算表、發 Telegram
"""

import json
import os
import sys
import logging
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from etf_registry import site_link   # 網站網址（GitHub Pages 已停用，舊網址 404）

# --------------- Config ---------------
API_URL = "https://www.esunam.com/ETFAPI/GetFundAssets"
ESUN_FUND_ID = "54"   # 玉山內部編號（GetETFOverview：StcokNo 00413A → FundNo 54）
HOLDINGS_DIR = "holdings"
ETF_CODE = "00413A"

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00413A.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, trade_amount, change_text, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00413A", name="主動玉山關鍵科技", manager="郭明玉")


# --------------- Helpers ---------------

def _num(s):
    return str(s).replace(",", "").replace("%", "").strip()


def fetch_holdings():
    """回傳 (holdings, aum_ntd, units, nav_date YYYY-MM-DD)；失敗回 (None, 0, 0, "")。"""
    payload = json.dumps({"FundID": ESUN_FUND_ID}).encode()
    req = urllib.request.Request(
        API_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    log.info(f"Fetching {API_URL} (FundID={ESUN_FUND_ID}) ...")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
        data = (result.get("Entries") or {}).get("Data") or {}
        fa = data.get("FundAsset") or {}
        nav_date = str(fa.get("NavDate", "")).strip().replace("/", "-")
        aum_ntd = int(float(_num(fa.get("Aum", "0")) or 0))
        units = int(float(_num(fa.get("Units", "0")) or 0))

        holdings = []
        for table in data.get("Table") or []:
            if str(table.get("TableTitle", "")).strip() != "股票":
                continue
            for row in table.get("Rows") or []:
                try:
                    holdings.append({
                        "code": str(row[0]).strip(),
                        "name": str(row[1]).strip(),
                        "shares": int(float(_num(row[2]))),
                        "weight": float(_num(row[3])),
                    })
                except Exception:
                    log.warning(f"Skip row: {row}")
        log.info(f"Got {len(holdings)} stocks, NavDate={nav_date}, "
                 f"AUM {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units {units:,}")
        return holdings, aum_ntd, units, nav_date
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None, 0, 0, ""


def valid_nav_date(nav_date, run_date):
    """NavDate 必須是 YYYY-MM-DD、交易日、且不晚於今天；否則不可寫入（不拿今天頂替）。"""
    try:
        d = datetime.strptime(nav_date, "%Y-%m-%d").date()
    except Exception:
        return False
    return d <= run_date and is_trading_day(d)


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: -abs(trade_amount(x, meta)))
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: -abs(trade_amount(x, meta)))
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00413A 主動玉山關鍵科技 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
        format_trade_line(wrapper),   # 💹 當日買超/賣超/淨額（etf_core，與日報同一套算法）
    ]
    if added:
        lines.append("\n新增持股：")
        for h in added:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'added')}")
    if removed:
        lines.append("\n出清持股：")
        for h in removed:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'removed')}")
    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'up')}")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{change_text(h, meta, 'down')}")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append(site_link(CFG.code))   # 網址單一來源（etf_registry），直接開到這一檔
    return "\n".join(lines)


def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    run_date_str = run_date.strftime("%Y-%m-%d")
    expected_str = prev_trading_day(run_date).strftime("%Y-%m-%d")

    log.info(f"=== 00413A Check & Update started ===")
    log.info(f"  Run date:  {run_date_str}")

    holdings, aum_ntd, units, nav_date = fetch_holdings()
    if not holdings or not valid_nav_date(nav_date, run_date):
        log.warning(f"玉山 API 無有效資料（NavDate={nav_date!r}，{len(holdings or [])} 檔），不寫入")
        if not holdings_exist_for(CFG, expected_str):
            send_telegram(f"⏳ 00413A 主動玉山關鍵科技 持股尚未更新\n📅 資料日期：{expected_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if holdings_exist_for(CFG, nav_date):
        # API 只給最新一天：已存過＝今天的還沒出來（或已處理），前一交易日有就不吵
        log.info(f"Holdings for {nav_date} already exist. Nothing to do.")
        if nav_date < expected_str:
            send_telegram(f"⏳ 00413A 主動玉山關鍵科技 持股尚未更新\n📅 資料日期：{expected_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    save_holdings(CFG, nav_date, holdings)
    prev_holdings = load_prev_holdings(CFG, nav_date)
    wrapper = build_data_json(CFG, holdings, prev_holdings, nav_date, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
