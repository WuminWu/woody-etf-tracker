"""
00418A ETF Holdings Daily Checker & Updater (主動復華台灣新星)

資料來源：復華投信 API（純 HTTP，與 00991A／00409A 同一支，內部代號 ETF27）
  https://www.fhtrust.com.tw/api/assetsExcel/ETF27/{YYYYMMDD}
  - 該日尚未公布：HTTP 200 但內容不是 xlsx（開頭不是 PK）
  - xlsx 第 3 列「日期: YYYY/MM/DD」→ 必須等於查詢日才寫入
  - **掛牌前（11/3 成立～11/10）只有淨資產／單位數，沒有持股表**；持股預計 11/11 掛牌後才揭露

首次揭露（holdings/ 還沒有任何一份）：
  比照 onboard_etf.py——前一日用同一份持股（不會整份顯示「新增」）、
  成本＝當日官方收盤（onboard_etf.seed_cost_basis，_lastProcessed＝當日），隔天起正常比較加減碼。
"""

import json
import os
import re
import sys
import logging
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import pandas as pd
from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from etf_registry import site_link   # 網站網址（GitHub Pages 已停用，舊網址 404）

# --------------- Config ---------------
API_BASE = "https://www.fhtrust.com.tw/api/assetsExcel/ETF27"
HOLDINGS_DIR = "holdings"
ETF_CODE = "00418A"

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("check_and_update_00418A.log", encoding="utf-8"),
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
CFG = FundConfig(code="00418A", name="主動復華台灣新星", manager="戎宜蘋")


# --------------- Helpers ---------------

def download_xlsx(date_str):
    """下載該日 xlsx（date_str YYYY-MM-DD）；尚未公布或失敗回 None。"""
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
    return tmp_path


def _num(v):
    return str(v).replace(",", "").replace("%", "").strip()


def parse_xlsx(xlsx_path):
    """回傳 (資料日期 YYYY-MM-DD 或 "", holdings, aum_ntd, units)。

    版面（同 00991A）：A 欄依序「日期: YYYY/MM/DD」「基金資產淨值」→下一列數值、
    「基金在外流通單位數」→下一列數值……；「證券代號」那列是持股表頭，其後每列
    證券代號、證券名稱、股數、金額、權重(%)。
    """
    df = pd.read_excel(xlsx_path, header=None, dtype=str)
    col0 = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[:, 0]]

    data_date, aum_ntd, units, header_row = "", 0, 0, None
    for i, cell in enumerate(col0):
        m = re.search(r"日期[:：]\s*(\d{4})/(\d{1,2})/(\d{1,2})", cell)
        if m and not data_date:
            data_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        nxt = col0[i + 1] if i + 1 < len(col0) else ""
        try:
            if cell == "基金資產淨值":
                aum_ntd = int(float(_num(nxt)))
            elif "流通單位數" in cell:
                units = int(float(_num(nxt)))
        except Exception:
            pass
        if cell == "證券代號" and header_row is None:
            header_row = i

    holdings = []
    if header_row is not None and df.shape[1] >= 5:
        for _, row in df.iloc[header_row + 1:].iterrows():
            code = "" if pd.isna(row.iloc[0]) else str(row.iloc[0]).strip()
            if not code:
                continue
            try:
                holdings.append({
                    "code": code,
                    "name": str(row.iloc[1]).strip(),
                    "shares": int(float(_num(row.iloc[2]))),
                    "weight": float(_num(row.iloc[4])),
                })
            except Exception:
                log.warning(f"Skip row: {list(row)}")
    log.info(f"xlsx 日期 {data_date or '?'}：{len(holdings)} 檔，AUM {aum_ntd:,}（{aum_ntd/1e8:.2f}億），Units {units:,}")
    return data_date, holdings, aum_ntd, units


def build_notification(wrapper, first_day=False):
    meta, holdings = wrapper["meta"], wrapper["holdings"]
    if first_day:
        top = sorted([h for h in holdings if h["shares"] > 0], key=lambda x: -x.get("todayWeight", 0))
        lines = [
            f"🆕 00418A 主動復華台灣新星 首次揭露持股",
            f"📅 資料日期：{meta['dataDate']}",
            f"💰 ETF 股價：{meta['etfPrice']}",
            f"📦 持股數量：{len(top)} 檔（成本以當日收盤建立，明天起比較加減碼）",
            "",
            "前十大：",
        ] + [f"  • {h['code']} {h['name']}　{h.get('todayWeight', 0):.2f}%" for h in top[:10]]
        lines.append(f"\n🕐 更新時間：{meta['lastUpdate']} (台灣時間)")
        lines.append(site_link(CFG.code))
        return "\n".join(lines)

    added     = [h for h in holdings if h.get("prevShares", 0) == 0 and h["shares"] > 0]
    removed   = [h for h in holdings if h["shares"] == 0 and h.get("prevShares", 0) > 0]
    increased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) > 0 and h.get("prevShares", 0) > 0], key=lambda x: -abs(trade_amount(x, meta)))
    decreased = sorted([h for h in holdings if h["shares"] > 0 and h.get("diffShares", 0) < 0], key=lambda x: -abs(trade_amount(x, meta)))
    ytd_sign = "+" if float(meta["ytd"]) >= 0 else ""
    lines = [
        f"📊 00418A 主動復華台灣新星 持股更新",
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


def try_date(date_str):
    """下載並解析某日；日期不符或沒有持股回 None，否則 (holdings, aum, units)。"""
    path = download_xlsx(date_str)
    if path is None:
        return None
    try:
        data_date, holdings, aum_ntd, units = parse_xlsx(path)
    finally:
        if os.path.exists(path):
            os.remove(path)
    if data_date != date_str:
        log.warning(f"xlsx 日期 {data_date!r} ≠ 查詢日 {date_str}，不寫入")
        return None
    if not holdings:
        log.info(f"{date_str} 只有淨值、尚無持股表（掛牌前不揭露）")
        return None
    return holdings, aum_ntd, units


def main():
    run_date = datetime.now(timezone(timedelta(hours=8))).date()
    run_date_str = run_date.strftime("%Y-%m-%d")
    prev_str = prev_trading_day(run_date).strftime("%Y-%m-%d")

    log.info(f"=== 00418A Check & Update started ===")
    log.info(f"  Run date:  {run_date_str}")

    # 復華當天收盤後就會公布當日持股 → 先試今天，沒有再試前一交易日
    candidates = ([run_date_str] if is_trading_day(run_date) else []) + [prev_str]
    got, data_date_str = None, ""
    for d in candidates:
        if holdings_exist_for(CFG, d):
            log.info(f"Holdings for {d} already exist. Nothing to do.")
            return
        got = try_date(d)
        if got:
            data_date_str = d
            break
    if not got:
        if prev_str >= "2026-11-11":   # 掛牌（11/11）前本來就沒有持股，不必提醒
            send_telegram(f"⏳ 00418A 主動復華台灣新星 持股尚未更新\n📅 資料日期：{prev_str}\n🔄 將於 30 分鐘後再次檢查...")
        return
    holdings, aum_ntd, units = got

    save_holdings(CFG, data_date_str, holdings)
    prev_holdings = load_prev_holdings(CFG, data_date_str)
    first_day = not prev_holdings
    if first_day:
        log.info("首次揭露持股：前一日用同一份持股，成本＝當日收盤")
        prev_holdings = holdings
    wrapper = build_data_json(CFG, holdings, prev_holdings, data_date_str, aum_ntd=aum_ntd, units=units)
    if first_day:
        from onboard_etf import seed_cost_basis
        prices = {h["code"]: h.get("price", 0) for h in wrapper["holdings"]}
        n = seed_cost_basis(CFG.code, data_date_str, holdings, lambda c: prices.get(c, 0))
        log.info(f"cost_basis.json：{n} 檔，成本＝{data_date_str} 收盤")
    append_holdings_to_sheets(ETF_CODE, wrapper["meta"]["dataDate"], wrapper["holdings"], meta=wrapper["meta"])

    send_update_notification(CFG.code, build_notification(wrapper, first_day), wrapper["meta"].get("totalMarketCap", 0))
    log.info("=== Done! ===")


if __name__ == "__main__":
    main()
