"""
00997A ETF Holdings Daily Checker & Updater (群益美國增長主動式ETF)

資料來源類型 D（capitalfund，Playwright + Angular）+ 海外股處理：
  https://www.capitalfund.com.tw/etf/product/detail/502/portfolio
  - 與 00992A/00982A 同平台：設日期 → 下載 Excel
  - 「參股」分頁欄位：股票代號 / 股票名稱 / 投資權重(%) / 股數
  - 持股為美股為主的全球股，代號為「TICKER 市場」格式（MU US、4062 JP、009150 KS、
    2330 為台股無後綴、BESI NA…）→ 價格用 00988A 式 market_map 對應 yfinance
  - 「投資組合」分頁含基金淨資產價值 / 已發行受益權單位總數

注意：此為海外/台美混合 ETF，daily_digest.py 已歸入 OVERSEAS_ETFS 群組（與 00988A 同一份報告）。
2026/4/14 掛牌（IPO 價 10），update_prices.py IPO_BASELINE 已設定。
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

import pandas as pd
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from market_utils import yf_symbol, ccy_of

# --------------- Config ---------------
FUND_URL = "https://www.capitalfund.com.tw/etf/product/detail/502/portfolio"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00997A.json"
ETF_CODE = "00997A"
ETF_NAME = "群益美國增長"
MANAGER = "吳承恕"
IPO_DATE = "2026-04-14"
IPO_PRICE = 10.0

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00997A.log", encoding="utf-8"),
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
from capitalfund import download_holdings_xlsx   # 群益三支共用下載器（含來源未揭露偵測）

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00997A", name="主動群益美國增長", manager="吳承恕",
                 ipo_date="2026-04-14", ipo_price=10.0)


def download_xlsx(date_str):
    """下載查詢日 date_str（yyyy/mm/dd）的持股 xlsx；來源尚未揭露回傳 None。

    實作在 capitalfund.py（00982A/00992A/00997A 共用，含「查無資料」對話框偵測，
    避免點到被對話框攔截的下載鈕而白等 30 秒逾時並拋例外）。
    """
    return download_holdings_xlsx(
        FUND_URL, date_str, os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx"))
def parse_holdings_from_xlsx(xlsx_path):
    """參股分頁（index 1）：代號(0)/名稱(1)/權重(2)/股數(3)。代號可為美股 TICKER（無數字）。"""
    df = pd.read_excel(xlsx_path, sheet_name=1, header=0)
    holdings = []
    for _, row in df.iterrows():
        code = str(row.iloc[0]).strip()
        name = str(row.iloc[1]).strip()
        weight_str = str(row.iloc[2]).strip().replace("%", "")
        shares_str = str(row.iloc[3]).strip().replace(",", "")
        if not code or code in ("nan", "股票代號"):
            continue
        try:
            shares = int(float(shares_str))
            weight = float(weight_str)
        except ValueError:
            continue
        holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
    return holdings


def parse_aum_from_xlsx(xlsx_path):
    """投資組合分頁（index 0）：基金淨資產價值 / 已發行受益權單位總數。"""
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
        log.warning(f"AUM parse failed: {e}")
        return 0, 0


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
    data_date_str = run_date.strftime("%Y-%m-%d")
    form_date_str = next_trading_day(run_date).strftime("%Y/%m/%d")   # 輸入 T+1 取得 T 資料

    log.info(f"=== {ETF_CODE} Check & Update started ===")
    log.info(f"  Run/Data date: {data_date_str}　Form date: {form_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    xlsx_path = download_xlsx(form_date_str)
    if xlsx_path is None:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    aum_ntd, units = parse_aum_from_xlsx(xlsx_path)
    if not today_holdings:
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 {data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return
    log.info(f"Parsed {len(today_holdings)} holdings for {data_date_str}")

    os.replace(xlsx_path, os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx"))
    with open(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
