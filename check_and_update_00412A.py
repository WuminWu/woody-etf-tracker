"""
00412A ETF Holdings Daily Checker & Updater (主動安聯亞洲半導體)

資料來源：安聯投信 ETF 專區（與 00993A 同一平台，基金代號 E0004）
  https://etf.allianzgi.com.tw/etf-info/E0004?tab=4
  - API 有 CSRF 保護，直接 HTTP 會 400 → Playwright 開頁面、攔截 GetFundAssets / GetFundDetail。
  - API 只回「最新一天」，不接受日期參數（實測 SearchDate / Date / NavDate 皆被忽略）。
    所以只要出現新的 NavDate 就存下來 —— 錯過的那天之後補不回來。
  - 表格依標題辨識，不可沿用 00993A 的位置假設：00993A 的 Table[2] 是台指期貨，
    00412A 的 Table[2] 是「附買回債券」（RP，只有金額沒有股數），不是持股。
  - 持股為亞洲／全球股：台股無後綴，海外為「TICKER 市場」（JP / US / KS…）。
    陸股用 Bloomberg 交易所碼 C1（上海）/ C2（深圳），一律轉成 CH：
    下游的 market_utils.yf_symbol、is_stock_code 與日報只認得 CH，
    不轉的話陸股會查不到股價、也不會被算進個股統計。

T+1：持有日股／美股，安聯的申購買回清單比台股 ETF 晚一個交易日
（實測 2026-10-14 上午仍為 10/12 的持股；00993A 是當天 18:30 前後就更新）。
歸入海外/混合組。2026/9/23 成立、2026/10/13 掛牌（IPO 價 10），不配息（累積型）。
"""

import json
import logging
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from asset_allocation import format_scale_line

# --------------- Config ---------------
ETF_CODE = "00412A"
ETF_NAME = "主動安聯亞洲半導體"
PAGE_URL = "https://etf.allianzgi.com.tw/etf-info/E0004?tab=4"
HOLDINGS_DIR = "holdings"

# Bloomberg 的中國交易所碼 → 本專案統一的市場碼
MARKET_ALIAS = {"C1": "CH", "C2": "CH"}

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
    FundConfig, build_data_json, fmt_zhang, format_trade_line, today_tw,
    holdings_exist_for, load_prev_holdings, save_holdings,
    is_trading_day, prev_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00412A", name="主動安聯亞洲半導體", manager="蕭惠中",
                 ipo_date="2026-10-13", ipo_price=10.0)


# --------------- Allianz API Fetcher ---------------

def fetch_fund_assets():
    """開安聯 ETF 頁面，攔截 GetFundAssets（持股＋規模）與 GetFundDetail（經理人）。"""
    captured = {}

    def handle_response(response):
        url = response.url
        if "GetFundAssets" in url:
            try:
                captured["data"] = response.json()
                log.info(f"Captured holdings API: {url}")
            except Exception as e:
                log.warning(f"Failed to parse holdings response: {e}")
        elif "GetFundDetail" in url:
            try:
                manager = response.json().get("Entries", {}).get("CManager", "")
                if manager:
                    captured["manager"] = manager
                    log.info(f"Captured manager: {manager}")
            except Exception as e:
                log.warning(f"Failed to parse GetFundDetail response: {e}")

    log.info(f"Launching Playwright to fetch {PAGE_URL} ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="zh-TW",
        )
        page = context.new_page()
        page.on("response", handle_response)
        try:
            # tab=1 觸發 GetFundDetail（經理人），tab=4 觸發 GetFundAssets（持股）
            page.goto(PAGE_URL.replace("tab=4", "tab=1"), wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4000)
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(8000)
        except Exception as e:
            log.warning(f"Page load issue (non-fatal): {e}")
        browser.close()

    return captured.get("data"), captured.get("manager")


def normalize_code(code_raw):
    """'600183 C1' → '600183 CH'；'6857 JP'、'2330' 原樣。"""
    parts = code_raw.split()
    if len(parts) == 2:
        market = parts[1].upper()
        return f"{parts[0]} {MARKET_ALIAS.get(market, market)}"
    return code_raw.strip()


def parse_fund_assets(raw_data):
    """回傳 (資料日 YYYY-MM-DD, 淨資產 NTD, 在外流通單位數, holdings[])。"""
    try:
        entries = raw_data["Entries"]["Data"]
        fa = entries["FundAsset"]
        tables = entries["Table"]
    except (KeyError, TypeError) as e:
        log.error(f"Unexpected API structure: {e}")
        return "", 0, 0, []

    try:
        aum_ntd = int(float(str(fa.get("Aum", "0")).replace(",", "")))
        units = int(float(str(fa.get("Units", "0")).replace(",", "")))
    except ValueError as e:
        log.warning(f"FundAsset parse error: {e}")
        aum_ntd, units = 0, 0
    # NavDate 才是持股的資料日；PCFDate 是「下一次申購買回籃子」的日期，會比今天還晚
    data_date = str(fa.get("NavDate", "")).replace("/", "-")

    holdings, stock_tables = [], 0
    for t in tables:
        title = str(t.get("TableTitle") or "").strip()
        if not title.startswith("股票"):
            if title:
                log.info(f"略過非股票表格：{title}（{len(t.get('Rows', []))} 列）")
            continue
        stock_tables += 1
        # 每列：[序號, 股票代號, 股票名稱, 股數, 權重%]
        for row in t.get("Rows", []):
            if len(row) < 5:
                continue
            code = normalize_code(str(row[1]))
            if not re.match(r"^[0-9A-Za-z]", code):
                continue
            try:
                shares = int(float(str(row[3]).replace(",", "")))
                weight = float(str(row[4]).replace("%", "").replace(",", ""))
            except ValueError:
                continue
            if shares <= 0:
                continue
            holdings.append({"code": code, "name": str(row[2]).strip(), "shares": shares, "weight": weight})

    if stock_tables == 0:
        log.error(f"找不到「股票」表格，版面可能變動。表格標題：{[t.get('TableTitle') for t in tables]}")
    log.info(f"資料日 {data_date}；AUM {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億)；Units {units:,}；持股 {len(holdings)} 檔")
    return data_date, aum_ntd, units, holdings


# --------------- Helpers ---------------

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
        format_trade_line(wrapper),   # 💹 當日買超/賣超/淨額（etf_core，與日報同一套算法）
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
    run_date = today_tw()
    target_str = prev_trading_day(run_date).strftime("%Y-%m-%d")   # T+1：今晚應拿到前一交易日的持股
    log.info(f"=== {ETF_CODE} Check & Update started. Run {run_date}, expected data date {target_str} ===")
    pending_msg = f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{target_str}\n🔄 將於 30 分鐘後再次檢查..."

    raw_data, manager = fetch_fund_assets()
    if not raw_data:
        log.error("No data captured from Allianz API.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 無法取得持股資料\n🔄 請檢查 Playwright 抓取是否正常")
        return

    data_date_str, aum_ntd, units, today_holdings = parse_fund_assets(raw_data)
    try:
        data_date = datetime.strptime(data_date_str, "%Y-%m-%d").date()
    except ValueError:
        log.error(f"NavDate 無法解析：{data_date_str!r}")
        send_telegram(pending_msg)
        return
    # 不像 00993A 把異常日期硬改成前一交易日：那會把別天的持股存成錯的日期
    if data_date > run_date or not is_trading_day(data_date):
        log.error(f"NavDate {data_date_str} 為未來日或非交易日，不寫入。")
        return

    if holdings_exist_for(CFG, data_date_str):
        if data_date_str < target_str:
            log.info(f"來源仍是 {data_date_str}（已存），{target_str} 尚未公布。")
            send_telegram(pending_msg)
        else:
            log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    if not today_holdings:
        log.error("解析不到任何持股，版面可能變動。")
        send_telegram(f"⚠️ {ETF_CODE} {ETF_NAME} 解析不到持股\n📅 資料日期：{data_date_str}\n🔧 安聯網站版面可能變動，請檢查爬蟲")
        return

    # 來源只留最新一天：即使比預期舊（例如前一晚漏跑），沒存過就先存，否則永久遺失
    if data_date_str != target_str:
        log.warning(f"資料日 {data_date_str} 與預期 {target_str} 不同，仍照實存檔。")

    save_holdings(CFG, data_date_str, today_holdings)
    prev_holdings = load_prev_holdings(CFG, data_date_str)
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str,
                              aum_ntd=aum_ntd, units=units, manager=manager)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])
    send_update_notification(CFG.code, build_notification(wrapper), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
