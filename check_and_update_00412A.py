"""
00412A ETF Holdings Daily Checker & Updater (主動安聯亞洲半導體)

資料來源：安聯投信 ETF 專區（與 00993A 同一平台，基金代號 E0004）
  https://etf.allianzgi.com.tw/etf-info/E0004?tab=4
  抓取與解析在 allianz.py（00412A / 00402A 共用）：Playwright 攔截 XHR、表格依標題辨識
  （本基金 Table[2] 是附買回債券，不是持股）、陸股 C1/C2 轉成 CH、API 只回最新一天。

持股為台股＋日美韓陸股：台股無後綴，海外為「TICKER 市場」，股價經 etf_core 換算台幣。

資料日（重要）：rule="pcf" —— 持股日 ＝ PCFDate 的前一交易日 ＝ 公布當天，不是 NavDate。
  例：2026-10-14 開盤時頁面為 NavDate 10/12、PCFDate 10/14 → 這是 10/13 的持股；
  10/14 收盤後才會出現 10/14 的持股（使用者確認）。
  基金規模（Aum / Units）是 NavDate 那天的數字，所以申購贖回反映在規模上會晚一天。

歸入海外/混合組。2026/9/23 成立、2026/10/13 掛牌（IPO 價 10），不配息（累積型）。
"""

import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sheets_helper import append_holdings_to_sheets
from notify import send_telegram, send_update_notification   # 單一來源：節流＋429重試＋自動分段；持股更新通知排程時依規模排序
from asset_allocation import format_scale_line
from allianz import fetch_fund_assets, parse_fund_assets, expected_date   # 安聯兩支共用

# --------------- Config ---------------
ETF_CODE = "00412A"
ETF_NAME = "主動安聯亞洲半導體"
FUND_NO = "E0004"
DATE_RULE = "pcf"   # 持股日＝PCFDate 前一交易日（見 allianz.py）
HOLDINGS_DIR = "holdings"

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
    holdings_exist_for, load_prev_holdings, save_holdings, is_trading_day,
)

# --- 本基金設定：所有與其他基金不同之處都集中在這裡 ---
CFG = FundConfig(code="00412A", name="主動安聯亞洲半導體", manager="蕭惠中",
                 ipo_date="2026-10-13", ipo_price=10.0)


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
    target_str = expected_date(run_date, DATE_RULE).strftime("%Y-%m-%d")
    log.info(f"=== {ETF_CODE} Check & Update started. Run {run_date}, expected data date {target_str} ===")
    pending_msg = f"⏳ {ETF_CODE} {ETF_NAME} 持股尚未更新\n📅 資料日期：{target_str}\n🔄 將於 30 分鐘後再次檢查..."

    raw_data, manager = fetch_fund_assets(FUND_NO)
    if not raw_data:
        log.error("No data captured from Allianz API.")
        send_telegram(f"⏳ {ETF_CODE} {ETF_NAME} 無法取得持股資料\n🔄 請檢查 Playwright 抓取是否正常")
        return

    data_date_str, aum_ntd, units, today_holdings = parse_fund_assets(raw_data, DATE_RULE)
    try:
        data_date = datetime.strptime(data_date_str, "%Y-%m-%d").date()
    except ValueError:
        log.error(f"持股日無法判定（NavDate / PCFDate 皆無法解析）：{data_date_str!r}")
        send_telegram(pending_msg)
        return
    # 不像 00993A 把異常日期硬改成前一交易日：那會把別天的持股存成錯的日期
    if data_date > run_date or not is_trading_day(data_date):
        log.error(f"持股日 {data_date_str} 為未來日或非交易日，不寫入。")
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
