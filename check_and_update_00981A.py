"""
00981A ETF Holdings Daily Checker & Updater

Logic:
1. Download the holdings XLSX from ezmoney.com.tw
2. Check if the date in the Excel matches today
3. If YES → save it, compare with previous day's holdings, generate data_00981A.json, push to GitHub
4. If NO → exit (Task Scheduler will retry next hour)
5. If today's file already exists → skip entirely (already done for today)
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
# --- 共用核心（等價重構，見 etf_core.py 檔頭）---
from etf_core import (
    FundConfig, build_data_json, get_price, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day, next_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00981A", name="統一台股增長", manager="陳釧瑤",
                 has_asset_alloc=True)

from asset_allocation import parse_asset_allocation, format_alloc_lines, find_prev_alloc, attach_delta, format_scale_line

# --------------- Config ---------------
FUND_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW"
HOLDINGS_DIR = "holdings"
DATA_FILE = "data_00981A.json"

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

        # Find and click export button
        export_btn = page.locator("button:has-text('匯出XLSX')")
        if export_btn.count() == 0:
            export_btn = page.locator("button:has-text('匯出')")
        if export_btn.count() == 0:
            export_btn = page.locator("a:has-text('匯出XLSX')")

        if export_btn.count() == 0:
            browser.close()
            log.error("Cannot find export button!")
            return None, None

        with page.expect_download(timeout=30000) as download_info:
            export_btn.first.click()
            log.info("Clicked export button, waiting for download...")

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

    版面容錯：ezmoney xlsx 版面偶爾位移一列，過去寫死「第 19 列起」會把標題列
    （股票代碼/股數/權重）當成持股解析而崩潰。改為從第 15 列起掃描，且只接受
    「代號為 4~6 碼數字(可帶一碼英文) 且股數可轉為整數」的列，其餘（標題/空白/
    合計列）自動略過，位移一兩列也能正確解析。
    """
    import re
    df = pd.read_excel(xlsx_path)
    stock_data = []
    start = max(0, min(15, len(df)))
    for idx in range(start, len(df)):
        row = df.iloc[idx]
        code = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        name = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        shares_str = str(row.iloc[2]).strip().replace(",", "") if pd.notna(row.iloc[2]) else ""
        weight_str = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else "0%"
        if not re.fullmatch(r"\d{4,6}[A-Za-z]?", code):   # 非股票代號（標題/合計等）→ 跳過
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


def build_notification(wrapper, etf_code="00981A", etf_name="統一台股增長"):
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
        format_trade_line(wrapper),   # 💰 當日買超/賣超/淨額（etf_core，與日報同一套算法）
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
    log.info(f"=== Check & Update started. Today: {today_str} ===")

    # 1. Skip if today already done
    if holdings_exist_for(CFG, today_tw().strftime("%Y-%m-%d")):
        log.info("Today's holdings already downloaded. Nothing to do.")
        return

    # 2. Download XLSX and check date
    xlsx_path, file_date = download_xlsx()

    if xlsx_path is None:
        log.error("Download failed. Will retry next hour.")
        send_telegram(f"⏳ 00981A 統一台股增長 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date is None:
        log.error("Could not parse date from file. Will retry next hour.")
        send_telegram(f"⏳ 00981A 統一台股增長 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date != today:
        log.info(f"File date ({file_date}) != today ({today}). Not yet updated. Will retry next hour.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ 00981A 統一台股增長 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 3. Date matches today! Save and process
    log.info(f"File date matches today! Processing...")

    # Save XLSX with proper name（os.replace 可覆蓋既有檔，避免 Windows rename 目標已存在時 WinError 183）
    final_xlsx = os.path.join(HOLDINGS_DIR, f"00981A_holdings_{today_str}.xlsx")
    os.replace(xlsx_path, final_xlsx)

    # Parse today's holdings
    today_holdings = parse_holdings_from_xlsx(final_xlsx)
    log.info(f"Parsed {len(today_holdings)} stocks from today's holdings")
    aum_ntd, units = parse_aum_from_xlsx(final_xlsx)
    asset_alloc = parse_asset_allocation(final_xlsx)
    if asset_alloc:
        attach_delta(asset_alloc, find_prev_alloc(HOLDINGS_DIR, "00981A", file_date.strftime("%Y-%m-%d")))
        log.info(f"資產配置: 股票 {asset_alloc.get('stockPct')}% / 現金 {asset_alloc.get('cashPct')}% / 期貨 {asset_alloc.get('futuresNotionalPct')}% / Δ={asset_alloc.get('delta')}")

    # Save as JSON
    json_path = os.path.join(HOLDINGS_DIR, f"00981A_holdings_{today_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    # 4. Load previous day's holdings and generate diff
    prev_holdings = load_prev_holdings(CFG, today_tw().strftime("%Y-%m-%d"))
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, file_date.strftime("%Y-%m-%d"), aum_ntd=aum_ntd, units=units, asset_alloc=asset_alloc)
    append_holdings_to_sheets("00981A", wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    # 5. Send Telegram notification (git push handled by GitHub Actions workflow)
    msg = build_notification(wrapper, etf_code="00981A", etf_name="統一台股增長")
    send_update_notification(CFG.code, msg, wrapper["meta"].get("totalMarketCap", 0))

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
