"""
00988A ETF Holdings Daily Checker & Updater (主動統一全球創新)

Special: This ETF holds overseas (US) stocks. Holdings for trading day T are only
published after T's US market close (~04:00–05:00 AM Taiwan time the next day).
Therefore this script always lags the Taiwan ETF scripts by one trading day:
  - Other Taiwan ETFs: dataDate = today (T) after 3 PM close
  - 00988A (global):  dataDate = yesterday (T-1); today's data available tomorrow

Data source: ezmoney.com.tw (XLSX download via Playwright)

Logic:
1. Download the holdings XLSX from ezmoney.com.tw
2. Accept file_date == prev_trading_day ±2 days (global ETF convention)
3. Always use prev_trading_day (prev_str) as canonical dataDate to align with official source
4. Save holdings, compare with previous day, generate data_00988A.json
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
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from market_utils import yf_symbol, ccy_of
from asset_allocation import parse_asset_allocation, format_alloc_lines, find_prev_alloc, attach_delta, format_scale_line

# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00988A", name="統一全球創新", manager="陳意婷",
                 has_asset_alloc=True)


# --------------- Config ---------------
FUND_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=61YTW"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00988A.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

if not os.path.exists(HOLDINGS_DIR):
    os.makedirs(HOLDINGS_DIR)


# --------------- Helpers ---------------

def minguo_to_date(minguo_str):
    """Convert Minguo date string like '115/04/17' to datetime.date"""
    parts = minguo_str.strip().split("/")
    year = int(parts[0]) + 1911
    month = int(parts[1])
    day = int(parts[2])
    return datetime(year, month, day).date()


def download_xlsx():
    """Download the XLSX from ezmoney and return (temp_path, date_in_file)."""
    tmp_path = os.path.join(HOLDINGS_DIR, "_temp_download.xlsx")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        log.info(f"Navigating to {FUND_URL} ...")
        # ezmoney 背景請求不斷，networkidle 常在 30s 內無法觸發而逾時；
        # 改用 domcontentloaded（DOM 載好即可），後續已有明確等待/點擊。
        page.goto(FUND_URL, wait_until="domcontentloaded")
        time.sleep(3)

        # Click 基金投資組合 tab
        portfolio_link = page.locator("a:has-text('基金投資組合')")
        if portfolio_link.count() > 0:
            portfolio_link.first.click()
            log.info("Clicked 基金投資組合 tab")
            page.wait_for_timeout(5000)
        else:
            log.warning("基金投資組合 tab not found, trying anchor link")
            page.goto(FUND_URL + "#asset", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

        # Scroll down to find export button
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Find and click export button inside #asset tab
        export_btn = page.locator("#asset button:has-text('匯出XLSX檔')")
        if export_btn.count() == 0:
            export_btn = page.locator("#asset button:has-text('匯出')")
        if export_btn.count() == 0:
            browser.close()
            log.error("Cannot find export button!")
            return None, None

        with page.expect_download(timeout=30000) as download_info:
            export_btn.first.evaluate("el => el.click()")
            log.info("Evaluated click on export button, waiting for download...")

        download = download_info.value
        download.save_as(tmp_path)
        original_name = download.suggested_filename
        log.info(f"Downloaded: {original_name}")
        browser.close()

    # Parse date from the Excel header (first column name is like "資料日:115/04/17")
    df = pd.read_excel(tmp_path)
    header_col = df.columns[0]  # e.g. "資料日:115/04/17"
    log.info(f"Excel header column: {header_col}")

    # Extract Minguo date
    if ":" in header_col or "：" in header_col:
        date_part = header_col.replace("：", ":").split(":")[-1].strip()
    else:
        date_part = header_col.strip()

    try:
        file_date = minguo_to_date(date_part)
        log.info(f"Date in file: {file_date}")
    except Exception as e:
        log.error(f"Failed to parse date from '{date_part}': {e}")
        return tmp_path, None

    return tmp_path, file_date


def parse_holdings_from_xlsx(xlsx_path):
    """Parse the holdings Excel into a list of dicts.

    版面容錯（2026-07-31）：ezmoney xlsx 版面偶爾位移一列，寫死「row 19 起」會把標題列
    當成持股解析而崩潰。改從第 15 列掃描；代號須以英數起首（排除中文標題列，00988A 為
    美股混合，代號如 'MU US'/'2330'/'285A JP' 皆保留），股數不可解析則略過。
    """
    import re
    df = pd.read_excel(xlsx_path)
    stock_data = []
    for idx in range(max(0, min(15, len(df))), len(df)):
        row = df.iloc[idx]
        code = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        shares_str = str(row.iloc[2]).strip().replace(",", "") if pd.notna(row.iloc[2]) else ""
        weight_str = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else "0%"
        if len(code) < 4 or code == "nan" or not re.match(r"^[0-9A-Za-z]", code):
            continue
        try:
            shares = int(float(shares_str))
        except ValueError:
            continue
        weight = float(weight_str.replace("%", "")) if "%" in weight_str else 0.0
        stock_data.append({"code": code, "name": name, "shares": shares, "weight": weight})
    return stock_data


def parse_aum_from_xlsx(xlsx_path):
    """Parse AUM from ezmoney XLSX header rows (before holdings at row 19)."""
    try:
        df = pd.read_excel(xlsx_path)
        aum_ntd, units = 0, 0
        for i in range(min(15, len(df))):
            row = df.iloc[i]
            cell0 = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            cell1 = str(row.iloc[1]) if len(row) > 1 and pd.notna(row.iloc[1]) else ""
            if "淨資產" in cell0 and cell1:
                aum_str = str(cell1).replace("NTD", "").replace(",", "").strip()
                try:
                    aum_ntd = int(float(aum_str))
                except Exception:
                    pass
            elif "流通在外單位數" in cell0 and cell1:
                try:
                    units = int(float(str(cell1).replace(",", "").strip()))
                except Exception:
                    pass
        log.info(f"AUM from XLSX: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
        return aum_ntd, units
    except Exception as e:
        log.warning(f"AUM parse from XLSX failed: {e}")
        return 0, 0


def build_notification(wrapper, etf_code="00988A", etf_name="主動統一全球創新"):
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
        format_trade_line(wrapper),   # 💹 當日買超/賣超/淨額（etf_core，與日報同一套算法）
    ]
    _extra = format_scale_line(meta) + format_alloc_lines(meta.get("assetAllocation"))
    if _extra:
        lines[4:4] = _extra   # 插在「持股數量」與空行之間：基金規模(贖回/申購) + 資產配置

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
    today = datetime.now(timezone(timedelta(hours=8))).date()
    today_str = today.strftime("%Y-%m-%d")
    prev_td = prev_trading_day(today_tw())
    prev_str = prev_td.strftime("%Y-%m-%d")
    log.info(f"=== Check & Update started. Today: {today_str}, checking for: {prev_str} ===")

    # 1. Skip if previous trading day already done
    if holdings_exist_for(CFG, prev_str):
        log.info(f"Holdings for {prev_str} already downloaded. Nothing to do.")
        return

    # 2. Download XLSX and check date
    xlsx_path, file_date = download_xlsx()

    if xlsx_path is None:
        log.error("Download failed. Will retry.")
        send_telegram(f"⏳ 00988A 主動統一全球創新 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date is None:
        log.error("Could not parse date from file. Will retry.")
        send_telegram(f"⏳ 00988A 主動統一全球創新 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 00988A 含海外（美股）成分，ezmoney 可能以台灣時間編製日期標記 XLSX（比實際交易日多1天）。
    # 因此同時接受 file_date == prev_trading_day（正常）及 file_date 超前1~2天（全球ETF慣例）。
    # 無論哪種情況，一律以 prev_str 作為 dataDate，與統一官方網站標示一致。
    date_delta = (file_date - prev_td).days
    if date_delta < 0 or date_delta > 2:
        log.info(f"File date ({file_date}) not compatible with prev trading day ({prev_td}) "
                 f"(delta={date_delta} days). Not yet updated.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ 00988A 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if date_delta > 0:
        log.info(f"File date ({file_date}) is {date_delta} day(s) ahead of prev trading day ({prev_td}). "
                 f"Using {prev_str} as canonical dataDate (aligns with official source).")

    # 3. Date matches (or is within tolerance)! Save and process
    log.info(f"File date acceptable for prev trading day ({prev_str})! Processing...")

    # Save XLSX with proper name
    final_xlsx = os.path.join(HOLDINGS_DIR, f"00988A_holdings_{prev_str}.xlsx")
    os.replace(xlsx_path, final_xlsx)

    # Parse today's holdings
    today_holdings = parse_holdings_from_xlsx(final_xlsx)
    log.info(f"Parsed {len(today_holdings)} stocks from today's holdings")
    aum_ntd, units = parse_aum_from_xlsx(final_xlsx)
    asset_alloc = parse_asset_allocation(final_xlsx)
    if asset_alloc:
        attach_delta(asset_alloc, find_prev_alloc(HOLDINGS_DIR, "00988A", prev_str))
        log.info(f"資產配置: 股票 {asset_alloc.get('stockPct')}% / 現金 {asset_alloc.get('cashPct')}% / 期貨 {asset_alloc.get('futuresNotionalPct')}% / Δ={asset_alloc.get('delta')}")

    # Save as JSON
    json_path = os.path.join(HOLDINGS_DIR, f"00988A_holdings_{prev_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    # 4. Load previous day's holdings and generate diff
    prev_holdings = load_prev_holdings(CFG, prev_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, prev_str, aum_ntd=aum_ntd, units=units, asset_alloc=asset_alloc)
    append_holdings_to_sheets("00988A", wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    # 5. Send Telegram notification (git push handled by GitHub Actions workflow)
    msg = build_notification(wrapper, etf_code="00988A", etf_name="主動統一全球創新")
    send_update_notification(CFG.code, msg, wrapper["meta"].get("totalMarketCap", 0))

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
