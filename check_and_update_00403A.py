"""
00403A ETF Holdings Daily Checker & Updater (主動統一升級50)

Data source: ezmoney.com.tw (fundCode=63YTW) — same flow as 00981A
Logic:
1. Download holdings XLSX from ezmoney.com.tw via Playwright
2. Verify the Minguo date in the file header matches today
3. If YES → save, compare with prev holdings, generate data_00403A.json
4. If NO  → exit (retry next scheduled run)
5. If today's file already exists → skip entirely
"""

import json
import os
import sys
import glob
import subprocess
import time
import logging
from datetime import date, datetime, timedelta, timezone

import urllib.request
import urllib.parse

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
import yfinance as yf
from playwright.sync_api import sync_playwright
from sheets_helper import append_holdings_to_sheets
from asset_allocation import parse_asset_allocation

# --------------- Config ---------------
ETF_CODE    = "00403A"
ETF_NAME    = "主動統一升級50"
MANAGER     = "統一投信"      # TODO: 確認正式經理人姓名後更新
FUND_URL    = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=63YTW"
HOLDINGS_DIR = "holdings"
DATA_FILE   = f"data_{ETF_CODE}.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

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


# --------------- Taiwan Market Holidays ---------------
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 28),
    date(2026, 5, 1),
    date(2026, 10, 10),
}


# --------------- Helpers ---------------

def minguo_to_date(minguo_str):
    """Convert Minguo date string like '115/05/11' to datetime.date."""
    parts = minguo_str.strip().split("/")
    year  = int(parts[0]) + 1911
    month = int(parts[1])
    day   = int(parts[2])
    return datetime(year, month, day).date()


def holdings_exist_for(date_str):
    return os.path.exists(os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json"))


def download_xlsx():
    """Download holdings XLSX from ezmoney and return (tmp_path, file_date)."""
    tmp_path = os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

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
            log.warning("基金投資組合 tab not found, trying anchor")
            page.goto(FUND_URL + "#asset", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Locate export button
        export_btn = page.locator("button:has-text('匯出XLSX')")
        if export_btn.count() == 0:
            export_btn = page.locator("button:has-text('匯出')")
        if export_btn.count() == 0:
            export_btn = page.locator("a:has-text('匯出XLSX')")

        if export_btn.count() == 0:
            browser.close()
            log.error("Cannot find export button!")
            return None, None

        with page.expect_download(timeout=30000) as dl_info:
            export_btn.first.click()
            log.info("Clicked export button, waiting for download...")

        dl = dl_info.value
        dl.save_as(tmp_path)
        log.info(f"Downloaded: {dl.suggested_filename}")
        browser.close()

    # Parse date from header cell (e.g. "資料日:115/05/11")
    df = pd.read_excel(tmp_path)
    header_col = df.columns[0]
    log.info(f"Excel header column: {header_col}")

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
    """Parse holdings from ezmoney XLSX（版面容錯：從第 15 列掃描，只收數字代號+可解析股數）。"""
    import re
    df = pd.read_excel(xlsx_path)
    stock_data = []
    for idx in range(max(0, min(15, len(df))), len(df)):
        row = df.iloc[idx]
        code      = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        name      = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        shares_str = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else "0"
        weight_str = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else "0%"

        if re.fullmatch(r"\d{4,6}[A-Za-z]?", code):
            try:
                shares = int(float(shares_str.replace(",", "")))
                weight = float(weight_str.replace("%", "")) if "%" in weight_str else 0.0
                stock_data.append({"code": code, "name": name, "shares": shares, "weight": weight})
            except Exception:
                pass
    return stock_data


def parse_aum_from_xlsx(xlsx_path):
    """Parse AUM and units from ezmoney XLSX header rows (rows 0–14)."""
    try:
        df = pd.read_excel(xlsx_path)
        aum_ntd, units = 0, 0
        for i in range(min(15, len(df))):
            row   = df.iloc[i]
            cell0 = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            cell1 = str(row.iloc[1]) if len(row) > 1 and pd.notna(row.iloc[1]) else ""
            if "淨資產" in cell0 and cell1:
                try:
                    aum_ntd = int(float(str(cell1).replace("NTD", "").replace(",", "").strip()))
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


def get_previous_holdings(exclude_date_str):
    """Find the most recent holdings JSON excluding the given date."""
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    files   = sorted(glob.glob(pattern))
    prev_files = [f for f in files if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        latest = prev_files[-1]
        log.info(f"Previous holdings file: {os.path.basename(latest)}")
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("No previous holdings file found.")
    return []


def get_price(code):
    for suffix in [".TW", ".TWO"]:
        try:
            hist = yf.Ticker(f"{code}{suffix}").history(period="1d", timeout=10)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=0, units=0, asset_alloc=None):
    """Compare today vs previous holdings, fetch prices, generate data_{ETF_CODE}.json."""
    prev_dict = {h["code"]: h for h in prev_holdings}
    prev_prices_map = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _prev = json.load(f)
            for ph in _prev.get("holdings", []):
                if ph.get("price", 0) > 0:
                    prev_prices_map[ph["code"]] = ph["price"]
        except Exception:
            pass

    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} stocks...")

    for i, h in enumerate(today_holdings):
        prev_data  = prev_dict.get(h["code"], {})
        shares_prev = prev_data.get("shares", 0)
        diff_shares = h["shares"] - shares_prev
        price       = get_price(h["code"])
        final_output.append({
            "code":       h["code"],
            "name":       h["name"],
            "shares":     h["shares"],
            "prevShares": shares_prev,
            "price":      round(price, 2),
            "prevPrice":  prev_prices_map.get(h["code"], 0),
            "yestWeight": prev_data.get("weight", 0.0),
            "todayWeight": h["weight"],
            "diffShares": diff_shares,
            "diffAmount": round(diff_shares * price, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            price       = get_price(prev_h["code"])
            diff_shares = -prev_h["shares"]
            final_output.append({
                "code":       prev_h["code"],
                "name":       prev_h["name"],
                "shares":     0,
                "prevShares": prev_h["shares"],
                "price":      round(price, 2),
                "prevPrice":  prev_prices_map.get(prev_h["code"], 0),
                "yestWeight": prev_h["weight"],
                "todayWeight": 0.0,
                "diffShares": diff_shares,
                "diffAmount": round(diff_shares * price, 2),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    # ETF price & YTD
    # 00403A 為 2026/5/12 掛牌新 ETF，掛牌價 10 元；YTD 以掛牌價為基準計算
    # (yfinance 回傳的「年內第一筆」收盤不見得等於 IPO 價，會造成 YTD 失真)
    IPO_DATE  = "2026-05-12"
    IPO_PRICE = 10.0
    ipo_year  = int(IPO_DATE.split("-")[0])
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = yf.Ticker(f"{ETF_CODE}.TW").history(period="ytd", timeout=10)
        if len(hist) >= 2:
            etf_price    = round(float(hist["Close"].iloc[-1]), 2)
            price_change = round(float((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100), 2)
            prev_price   = round(float(hist["Close"].iloc[-2]), 2)
            current_year = datetime.now(timezone(timedelta(hours=8))).year
            if current_year == ipo_year:
                ytd_val = f"{((etf_price - IPO_PRICE) / IPO_PRICE) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD (IPO baseline {IPO_PRICE}): {ytd_val}%")
            else:
                ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
                log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"Failed to fetch ETF price/YTD: {e}")

    # AUM
    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_raw = units if units > 0 else (round(aum_ntd / etf_price) if aum_ntd > 0 and etf_price > 0 else 0)

    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _prev = json.load(f)
            prev_meta = _prev.get("meta", {})
            _d = datetime.strptime(data_date_str, "%Y-%m-%d").date()
            _cand = _d - timedelta(days=1)
            while _cand.weekday() >= 5 or _cand in TW_MARKET_HOLIDAYS:
                _cand -= timedelta(days=1)
            _prev_td = _cand.strftime("%Y-%m-%d")
            if prev_meta.get("dataDate", "") == _prev_td:
                prev_total_shares     = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
            else:
                log.info(f"AUM 比較跳過：JSON dataDate={prev_meta.get('dataDate')} 非前一交易日({_prev_td})")
        except Exception:
            pass

    total_shares_zhang = total_shares_raw // 1000
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.1 or ratio > 5.0:
            log.warning(f"AUM 異常：{total_shares_zhang} vs 前一交易日 {prev_total_shares}（{ratio:.1%}），改用前值")
            total_shares_zhang    = prev_total_shares
            total_market_cap      = prev_total_market_cap
    if total_shares_zhang == 0 and prev_total_shares > 0:
        total_shares_zhang = prev_total_shares
        total_market_cap   = round(etf_price * prev_total_shares * 1000 / 1e8, 2) if etf_price > 0 else prev_total_market_cap

    wrapper = {
        "meta": {
            "manager":          MANAGER,
            "ytd":              ytd_val,
            "etfPrice":         etf_price,
            "priceChange":      price_change,
            "prevPrice":        prev_price,
            "dataDate":         data_date_str,
            "lastUpdate":       datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
            "totalShares":      total_shares_zhang,
            "prevTotalShares":  prev_total_shares,
            "totalMarketCap":   total_market_cap,
            "prevTotalMarketCap": prev_total_market_cap,
            "assetAllocation": asset_alloc or {},
        },
        "holdings": final_output,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=4)
    log.info(f"{DATA_FILE} updated with {len(final_output)} holdings")
    return wrapper


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            if json.loads(r.read()).get("ok"):
                log.info("Telegram notification sent.")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")


def fmt_zhang(shares):
    zhang = shares / 1000
    sign  = "+" if zhang > 0 else ""
    return f"{sign}{int(zhang):,}張" if zhang == int(zhang) else f"{sign}{zhang:,.1f}張"


def build_notification(wrapper):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: x["diffShares"], reverse=True)
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: x["diffShares"])
    ytd_sign  = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 {ETF_CODE} {ETF_NAME} 持股更新",
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
    today     = datetime.now(timezone(timedelta(hours=8))).date()
    today_str = today.strftime("%Y-%m-%d")
    log.info(f"=== {ETF_CODE} Check & Update started. Today: {today_str} ===")

    # 1. Skip if today's holdings already exist
    if holdings_exist_for(today_str):
        log.info(f"Holdings for {today_str} already exist. Nothing to do.")
        return

    # 2. Download XLSX and verify date in header
    xlsx_path, file_date = download_xlsx()

    if xlsx_path is None:
        log.error("Download failed. Will retry next run.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date is None:
        log.error("Could not parse date from XLSX. Will retry next run.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    if file_date != today:
        log.info(f"File date ({file_date}) != today ({today}). Holdings not yet updated.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{today_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    # 3. Date matches — save and process
    log.info("File date matches today! Processing...")
    final_xlsx = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{today_str}.xlsx")
    os.replace(xlsx_path, final_xlsx)

    today_holdings = parse_holdings_from_xlsx(final_xlsx)
    log.info(f"Parsed {len(today_holdings)} stocks")
    aum_ntd, units = parse_aum_from_xlsx(final_xlsx)
    asset_alloc = parse_asset_allocation(final_xlsx)
    if asset_alloc:
        log.info(f"資產配置: 股票 {asset_alloc.get('stockPct')}% / 現金 {asset_alloc.get('cashPct')}% / 期貨 {asset_alloc.get('futuresNotionalPct')}%")

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{today_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=today_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, today_str, aum_ntd=aum_ntd, units=units, asset_alloc=asset_alloc)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_telegram(build_notification(wrapper))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
