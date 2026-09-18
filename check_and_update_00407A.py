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
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段
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

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00407A", name="主動凱基台灣", manager="趙偉志",
                 ipo_date="2026-06-24", ipo_price=10.0)


# --------------- Helpers ---------------

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
        format_trade_line(wrapper),   # 💰 當日買超/賣超/淨額（etf_core，與日報同一套算法）
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
    if holdings_exist_for(CFG, data_date_str):
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

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
