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
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
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

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00409A", name="主動復華全球50", manager="胡家菱",
                 ipo_date="2026-09-02", ipo_price=10.0)


# --------------- Helpers ---------------

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

    if holdings_exist_for(CFG, data_date_str):
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

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
