"""
00991A ETF Holdings Daily Checker & Updater (復華未來50)

Logic:
1. Download holdings Excel from fhtrust.com.tw API (no Playwright needed)
   API: https://www.fhtrust.com.tw/api/assetsExcel/ETF23/{YYYYMMDD}
2. Parse holdings: 證券代號, 證券名稱, 股數, 權重(%)
3. Compare with previous day's holdings
4. Fetch stock prices via yfinance
5. Generate data_00991A.json
6. Push to GitHub
7. Send Telegram notification
"""

import json
import os
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

# --------------- Config ---------------
API_BASE = "https://www.fhtrust.com.tw/api/assetsExcel/ETF23"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00991A.json"
ETF_CODE = "00991A"
MANAGER = "呂宏宇"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00991A.log", encoding="utf-8"),
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
CFG = FundConfig(code="00991A", name="復華未來50", manager="呂宏宇")


# --------------- Helpers ---------------

def download_xlsx(date_str):
    """下載 fhtrust API 的持股 xlsx（date_str 格式 YYYY-MM-DD）；尚未揭露回傳 None。

    該日資料還沒公布時，API 回的是 HTTP 200 但內容不是 xlsx（短短的錯誤字串），
    存檔後交給 pandas 會炸：
        ValueError: Excel file format cannot be determined, you must specify an engine manually.
    例外往上拋 → 腳本非零結束，而其他爬蟲在這種情況是送出「持股尚未更新」後正常退出。
    所以這裡先檢查 zip magic（xlsx 就是 zip，開頭必為 PK），不是就當成尚未揭露。
    （00409A 用同一個 API，本來就有這道檢查，只是 00991A 寫得比較早。）
    """
    url = f"{API_BASE}/{date_str.replace('-', '')}"
    tmp_path = os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx")

    log.info(f"Downloading from {url} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None
    if raw[:2] != b"PK":
        log.info(f"{date_str} 尚未公布（回應 {len(raw)} bytes，非 xlsx）")
        return None
    with open(tmp_path, "wb") as f:
        f.write(raw)
    log.info(f"Downloaded to {tmp_path}")
    return tmp_path


def parse_holdings_from_xlsx(xlsx_path):
    """Parse holdings from fhtrust Excel. Columns: 證券代號, 證券名稱, 股數, 金額, 權重(%)"""
    df = pd.read_excel(xlsx_path, header=0)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        shares_str = str(row.iloc[2]).strip().replace(",", "")
        weight_str = str(row.iloc[4]).strip().replace("%", "")
        if code and code != "nan" and len(code) >= 4 and any(c.isdigit() for c in code):
            try:
                shares = int(float(shares_str))
                weight = float(weight_str)
                holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
            except Exception:
                pass
    return holdings


def parse_aum_from_xlsx(xlsx_path):
    """Parse AUM from fhtrust XLSX header rows."""
    try:
        df = pd.read_excel(xlsx_path, header=None)
        aum_ntd, units = 0, 0
        for i in range(min(15, len(df))):
            cell = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
            if "基金資產淨值" in cell or ("淨資產" in cell and "單位" not in cell):
                if i + 1 < len(df):
                    val = str(df.iloc[i + 1, 0]).replace(",", "").strip()
                    try:
                        aum_ntd = int(float(val))
                    except Exception:
                        pass
            elif "流通單位數" in cell or "在外流通" in cell:
                if i + 1 < len(df):
                    val = str(df.iloc[i + 1, 0]).replace(",", "").strip()
                    try:
                        units = int(float(val))
                    except Exception:
                        pass
        log.info(f"AUM from XLSX: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM parse from XLSX failed: {e}")
        return 0, 0


def build_notification(wrapper):
    meta = wrapper["meta"]
    holdings = wrapper["holdings"]

    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: x["diffShares"])

    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00991A 復華未來50 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
        format_trade_line(wrapper),   # 💰 當日買超/賣超/淨額（etf_core，與日報同一套算法）
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
    run_date = now.date()
    run_date_str = run_date.strftime("%Y-%m-%d")
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")

    log.info(f"=== 00991A Check & Update started ===")
    log.info(f"  Run date:  {run_date_str}")
    log.info(f"  Prev date: {data_date_str}")

    # fhtrust URL embeds the date; try today first (same-day publish after market close).
    # Fall back to previous trading day if today's file is not yet available.
    xlsx_path = download_xlsx(run_date_str)
    actual_date_str = run_date_str

    if xlsx_path is None:
        log.warning("Today's fhtrust data not available yet. Falling back to previous trading day.")
        if holdings_exist_for(CFG, data_date_str):
            log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
            return
        xlsx_path = download_xlsx(data_date_str)
        actual_date_str = data_date_str
        if xlsx_path is None:
            log.error("Download failed. Will retry next hour.")
            send_telegram(f"⏳ 00991A 復華未來50 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
            return
    else:
        if holdings_exist_for(CFG, actual_date_str):
            log.info(f"Holdings for {actual_date_str} already exist. Nothing to do.")
            if os.path.exists(xlsx_path):
                os.remove(xlsx_path)
            return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    aum_ntd, units = parse_aum_from_xlsx(xlsx_path)
    if not today_holdings:
        log.error("No holdings parsed. Will retry next hour.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ 00991A 復華未來50 持股尚未更新\n📅 資料日期：{actual_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    log.info(f"Parsed {len(today_holdings)} stocks for {actual_date_str}")

    final_xlsx = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{actual_date_str}.xlsx")
    os.replace(xlsx_path, final_xlsx)

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{actual_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, actual_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, actual_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    msg = build_notification(wrapper)
    send_telegram(msg)

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
