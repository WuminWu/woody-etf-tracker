"""
00982A ETF Holdings Daily Checker & Updater (群益台灣強棒)

Logic:
1. Navigate to capitalfund.com.tw/etf/product/detail/399/portfolio
2. Set date to today and download Excel
3. Parse holdings from Excel (sheet: 參股)
4. Compare with previous day's holdings
5. Fetch stock prices via yfinance
6. Generate data_00982A.json
7. Push to GitHub
"""

import json
import os
import sys
import time
import logging
from datetime import date, datetime, timedelta, timezone

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram   # 單一來源：節流＋429重試＋自動分段

# --------------- Config ---------------
FUND_URL = "https://www.capitalfund.com.tw/etf/product/detail/399/portfolio"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00982A.json"
ETF_CODE = "00982A"
MANAGER = "陳沅易"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00982A.log", encoding="utf-8"),
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
from capitalfund import download_holdings_xlsx   # 群益三支共用下載器（含來源未揭露偵測）

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00982A", name="群益台灣強棒", manager="陳沅易")


# --------------- Helpers ---------------

def download_xlsx(date_str):
    """下載查詢日 date_str（yyyy/mm/dd）的持股 xlsx；來源尚未揭露回傳 None。

    實作在 capitalfund.py（00982A/00992A/00997A 共用，含「查無資料」對話框偵測，
    避免點到被對話框攔截的下載鈕而白等 30 秒逾時並拋例外）。
    """
    return download_holdings_xlsx(
        FUND_URL, date_str, os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx"))
def parse_holdings_from_xlsx(xlsx_path):
    """Parse holdings from the Capital Fund Excel file (sheet index 1 = 參股)."""
    df = pd.read_excel(xlsx_path, sheet_name=1, header=0)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        weight_str = str(row.iloc[2]).strip().replace("%", "")
        shares_str = str(row.iloc[3]).strip().replace(",", "")
        if code and code != "nan" and len(code) >= 4 and any(c.isdigit() for c in code):
            try:
                shares = int(float(shares_str))
                weight = float(weight_str)
                holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
            except Exception:
                pass
    return holdings


def parse_aum_from_xlsx(xlsx_path):
    """Parse AUM from Sheet 0 ('投資組合') of the capitalfund XLSX."""
    try:
        df = pd.read_excel(xlsx_path, sheet_name=0, header=None)
        aum_ntd, units = 0, 0
        for i in range(len(df)):
            label = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
            value = str(df.iloc[i, 1]).strip() if pd.notna(df.iloc[i, 1]) else ""
            val_clean = value.replace("TWD", "").replace(",", "").strip()
            if "淨資產價值" in label and "單位" not in label and val_clean:
                aum_ntd = int(float(val_clean))
            elif "已發行受益權單位總數" in label and val_clean:
                units = int(float(val_clean))
        log.info(f"AUM from XLSX: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM parse from XLSX failed: {e}")
        return 0, 0


def build_notification(wrapper, etf_code="00982A", etf_name="群益台灣強棒"):
    """Build a summary notification message from the data wrapper."""
    meta = wrapper["meta"]
    holdings = wrapper["holdings"]

    added    = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed  = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted(
        [h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0],
        key=lambda x: x["diffShares"], reverse=True
    )
    decreased = sorted(
        [h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0],
        key=lambda x: x["diffShares"]
    )

    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 {etf_code} {etf_name} 持股更新",
        f"📅 資料日期：{meta['dataDate']}",
        f"💰 ETF 股價：{meta['etfPrice']}　　YTD：{ytd_sign}{meta['ytd']}%",
        f"📦 持股數量：{len([h for h in holdings if h['shares'] > 0])} 檔",
        "",
        f"🔴 加碼：{len(increased)} 檔　🟢 減碼：{len(decreased)} 檔",
        f"🟣 新增：{len(added)} 檔　🟠 出清：{len(removed)} 檔",
    ]

    if added:
        lines.append("\n✨ 新增持股：")
        for h in added:
            zhang = fmt_zhang(h["shares"])
            lines.append(f"  • {h['code']} {h['name']}　{zhang}（0% → {h['todayWeight']}%）")

    if removed:
        lines.append("\n🚫 出清持股：")
        for h in removed:
            zhang = fmt_zhang(-h.get("prevShares", 0))
            lines.append(f"  • {h['code']} {h['name']}　{zhang}")

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


def main():
    # Capital Fund date logic:
    #   Inputting date D on the website returns the PREVIOUS trading day's holdings.
    #   After market close on day T, capitalfund's site auto-defaults to T+1 (next trading day)
    #   and displays T's actual holdings.
    #
    # Script schedule (daily_update.yml): 15:00–19:30 TW time, AFTER 13:30 market close.
    # To fetch today's (T) holdings:
    #   - form_date = next_trading_day(today) = T+1  → returns T's holdings (D → D-1 rule)
    #   - data_date = today (T)                       → the actual data this XLSX represents
    #   - save file = data_date

    now = datetime.now(timezone(timedelta(hours=8)))
    run_date = now.date()
    data_date = run_date  # holdings represent today's (T) market close
    data_date_str = data_date.strftime("%Y-%m-%d")
    form_date_str = next_trading_day(run_date).strftime("%Y/%m/%d")  # input T+1 to receive T's data

    log.info(f"=== 00982A Check & Update started ===")
    log.info(f"  Run date (today):    {run_date}")
    log.info(f"  Form date (website): {form_date_str}")
    log.info(f"  Data date (actual):  {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    xlsx_path = download_xlsx(form_date_str)
    if xlsx_path is None:
        log.error("Download failed. Will retry next hour.")
        send_telegram(f"⏳ 00982A 群益台灣強棒 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    aum_ntd, units = parse_aum_from_xlsx(xlsx_path)
    if not today_holdings:
        log.error("No holdings parsed from Excel. Will retry next hour.")
        send_telegram(f"⏳ 00982A 群益台灣強棒 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    log.info(f"Parsed {len(today_holdings)} stocks for {data_date_str}")

    # Save with the ACTUAL data date
    final_xlsx = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx")
    os.replace(xlsx_path, final_xlsx)

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    msg = build_notification(wrapper, etf_code="00982A", etf_name="群益台灣強棒")
    send_telegram(msg)

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
