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
import glob
import subprocess
import time
import logging
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
import yfinance as yf
from sheets_helper import append_holdings_to_sheets

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


# --------------- Helpers ---------------

def prev_trading_day(date):
    d = date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def holdings_exist_for(date_str):
    filepath = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{date_str}.json")
    return os.path.exists(filepath)


def download_xlsx(date_str):
    """Download holdings Excel from fhtrust API. date_str format: YYYY-MM-DD"""
    date_nodash = date_str.replace("-", "")
    url = f"{API_BASE}/{date_nodash}"
    tmp_path = os.path.join(HOLDINGS_DIR, f"_{ETF_CODE}_temp.xlsx")

    log.info(f"Downloading from {url} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(tmp_path, "wb") as f:
                f.write(resp.read())
        log.info(f"Downloaded to {tmp_path}")
        return tmp_path
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None


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


def get_previous_holdings(exclude_date_str):
    pattern = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_*.json")
    files = sorted(glob.glob(pattern))
    prev_files = [f for f in files if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        latest = prev_files[-1]
        log.info(f"Previous holdings file: {os.path.basename(latest)}")
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        log.warning("No previous holdings file found.")
        return []


def get_price(code):
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = yf.Ticker(f"{code}{suffix}")
            hist = ticker.history(period="1d", timeout=10)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return 0.0


def generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=0, units=0):
    prev_dict = {h["code"]: h for h in prev_holdings}
    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} stocks...")

    for i, h in enumerate(today_holdings):
        code = h["code"]
        prev_data = prev_dict.get(code, {})
        shares_prev = prev_data.get("shares", 0)
        weight_prev = prev_data.get("weight", 0.0)
        diff_shares = h["shares"] - shares_prev
        price = get_price(code)
        final_output.append({
            "code": code,
            "name": h["name"],
            "shares": h["shares"],
            "prevShares": shares_prev,
            "price": round(price, 2),
            "yestWeight": weight_prev,
            "todayWeight": h["weight"],
            "diffShares": diff_shares,
            "diffAmount": round(diff_shares * price, 2),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] not in today_codes:
            price = get_price(prev_h["code"])
            diff_shares = -prev_h["shares"]
            final_output.append({
                "code": prev_h["code"],
                "name": prev_h["name"],
                "shares": 0,
                "prevShares": prev_h["shares"],
                "price": round(price, 2),
                "yestWeight": prev_h["weight"],
                "todayWeight": 0.0,
                "diffShares": diff_shares,
                "diffAmount": round(diff_shares * price, 2),
            })

    final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    ytd_val = "0.00"
    etf_price = 0.0
    try:
        t = yf.Ticker(f"{ETF_CODE}.TW")
        hist = t.history(period="ytd", timeout=10)
        if len(hist) >= 2:
            ytd_val = f"{((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100:.2f}"
            etf_price = round(float(hist["Close"].iloc[-1]), 2)
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}%")
    except Exception as e:
        log.warning(f"Failed to fetch ETF price/YTD: {e}")


    # AUM from official fhtrust XLSX; fallback to previous values if unavailable
    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    total_shares_raw = units if units > 0 else (round(aum_ntd / etf_price) if aum_ntd > 0 and etf_price > 0 else 0)
    # prevTotalShares：只在前一個交易日才做比較，避免腳本跳日造成跨多天誤差
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as _f:
                _prev = json.load(_f)
            prev_meta = _prev.get("meta", {})
            # 計算前一個交易日（跳過週末）
            _d = datetime.strptime(data_date_str, "%Y-%m-%d").date()
            _delta = 1
            while True:
                _candidate = _d - timedelta(days=_delta)
                if _candidate.weekday() < 5:
                    _prev_trading_day = _candidate.strftime("%Y-%m-%d")
                    break
                _delta += 1
            if prev_meta.get("dataDate", "") == _prev_trading_day:
                prev_total_shares = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
            else:
                log.info(f"AUM 比較跳過：JSON dataDate={prev_meta.get('dataDate')} 非前一交易日({_prev_trading_day})")
        except Exception:
            pass
    total_shares_zhang = total_shares_raw // 1000
    # AUM 合理性驗證：若新值與前一交易日相差超過 50%，視為資料來源解析異常，捨棄新值
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.5 or ratio > 2.0:
            log.warning(f"AUM 異常：totalShares={total_shares_zhang} 與前一交易日 {prev_total_shares} 相差 {ratio:.1%}，視為解析異常，改用前一交易日數值")
            total_shares_zhang = prev_total_shares
            total_market_cap = prev_total_market_cap
    # Fallback: if official source unavailable, keep previous values
    if total_shares_zhang == 0 and prev_total_shares > 0:
        total_shares_zhang = prev_total_shares
        total_market_cap = round(etf_price * prev_total_shares * 1000 / 1e8, 2) if etf_price > 0 else prev_total_market_cap
    wrapper = {
        "meta": {
            "manager": MANAGER,
            "ytd": ytd_val,
            "etfPrice": etf_price,
            "dataDate": data_date_str,
            "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
            "totalShares": total_shares_zhang,
            "prevTotalShares": prev_total_shares,
            "totalMarketCap": total_market_cap,
            "prevTotalMarketCap": prev_total_market_cap,
        },
        "holdings": final_output,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=4)

    log.info(f"{DATA_FILE} updated with {len(final_output)} holdings")
    return wrapper


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set. Skipping notification.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram notification sent.")
            else:
                log.warning(f"Telegram API error: {result}")
    except Exception as e:
        log.warning(f"Failed to send Telegram notification: {e}")


def fmt_zhang(shares):
    zhang = shares / 1000
    sign = "+" if zhang > 0 else ""
    if zhang == int(zhang):
        return f"{sign}{int(zhang):,}張"
    return f"{sign}{zhang:,.1f}張"


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
    ]
    if added:
        lines.append("\n新增持股：")
        for h in added:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['shares'])}（{h['todayWeight']}%）")
    if removed:
        lines.append("\n出清持股：")
        for h in removed:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(-h.get('prevShares', 0))}")
    if increased:
        lines.append("\n🔴 加碼明細：")
        for h in increased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['todayWeight']}%）")
    if decreased:
        lines.append("\n🟢 減碼明細：")
        for h in decreased:
            lines.append(f"  • {h['code']} {h['name']}　{fmt_zhang(h['diffShares'])}（{h['todayWeight']}%）")
    lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
    lines.append("https://wuminwu.github.io/woody-etf-tracker/")
    return "\n".join(lines)


def git_push():
    try:
        subprocess.run(["git", "add", "-A"], check=True)
        msg = f"Auto-update 00991A holdings {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "commit", "-m", msg], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Git push completed successfully.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")


# --------------- Main ---------------

def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    run_date = now.date()
    data_date = prev_trading_day(run_date)
    data_date_str = data_date.strftime("%Y-%m-%d")

    log.info(f"=== 00991A Check & Update started ===")
    log.info(f"  Run date:  {run_date}")
    log.info(f"  Data date: {data_date_str}")

    if holdings_exist_for(data_date_str):
        log.info(f"Holdings for {data_date_str} already exist. Nothing to do.")
        return

    xlsx_path = download_xlsx(data_date_str)
    if xlsx_path is None:
        log.error("Download failed. Will retry next hour.")
        send_telegram(f"⏳ 00991A 復華未來50 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    today_holdings = parse_holdings_from_xlsx(xlsx_path)
    aum_ntd, units = parse_aum_from_xlsx(xlsx_path)
    if not today_holdings:
        log.error("No holdings parsed. Will retry next hour.")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        send_telegram(f"⏳ 00991A 復華未來50 持股尚未更新\n📅 資料日期：{data_date_str}\n🔄 將於 30 分鐘後再次檢查...")
        return

    log.info(f"Parsed {len(today_holdings)} stocks for {data_date_str}")

    final_xlsx = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.xlsx")
    os.rename(xlsx_path, final_xlsx)

    json_path = os.path.join(HOLDINGS_DIR, f"{ETF_CODE}_holdings_{data_date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(today_holdings, f, ensure_ascii=False, indent=2)

    prev_holdings = get_previous_holdings(exclude_date_str=data_date_str)
    wrapper = generate_data_json(today_holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    msg = build_notification(wrapper)
    send_telegram(msg)

    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
