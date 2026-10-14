"""
00416A ETF Holdings Daily Checker & Updater (主動台新臺灣中小)

Logic:
1. Fetch static HTML from https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00416A
2. Parse holdings table: 股票代號, 名稱, 股數, 持股權重
   (stock codes have " TT" suffix — stripped on parse)
   Holdings date = 頁面「YYYY/M/D每基數實際申購總價金」的日期（申購買回清單日 PUB_DATE 是 T+1，
   清單內容是 T 日收盤後部位）；不是今天 → 官網尚未更新，不寫入（同站的 00987A 直接用今天，無此防護）
3. Compare with previous day's holdings
4. Fetch stock prices via yfinance
5. Generate data_00416A.json
6. Push to GitHub
7. Send Telegram notification
"""

import json
import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
import tsit   # 台新官網抓取／解析／持股日驗證（00987A、00416A 共用）
from etf_registry import site_link   # 網站網址（GitHub Pages 已停用，舊網址 404）
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序

# --------------- Config ---------------
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00416A.json"
ETF_CODE = "00416A"
MANAGER = "沈建宏"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00416A.log", encoding="utf-8"),
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
CFG = FundConfig(code="00416A", name="主動台新臺灣中小", manager="沈建宏",
                 aum_derive_check=True,   # 同站 00987A 來源張數偶爾錯，與 規模/淨值 反推值互相校驗
                 yf_suffix=".TW")   # 掛牌日/發行價取自 etfs.json


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: -abs(trade_amount(x, meta)))
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: -abs(trade_amount(x, meta)))
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00416A 主動台新臺灣中小 持股更新",
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
    # 收盤後官網換成「T+1 申購買回清單」＝今天收盤後的持股；預期持股日就是今天（tsit.fetch 會驗證）
    data_date_str = run_date.strftime("%Y-%m-%d")

    log.info(f"=== 00416A Check & Update started ===")
    log.info(f"  Run date / Data date: {data_date_str}")

    if holdings_exist_for(CFG, data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    today_holdings, aum_ntd, units = tsit.fetch(ETF_CODE, data_date_str)
    if not today_holdings:
        log.error("No holdings fetched. Will retry next hour.")
        send_telegram(f"⏳ 00416A 主動台新臺灣中小 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
