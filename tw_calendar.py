# -*- coding: utf-8 -*-
"""
tw_calendar.py — 台股交易日曆（全專案單一來源）。

各爬蟲原本各自抄一份休市日表，17/19 支缺了 12 天，已實際造成資料遺失：
  - 00988A 缺 4/30、6/18、7/9（都是休市日前一天）：T+1 爬蟲把休市日當成前一交易日，
    永遠等不到那天的檔案，而統一的網站只保留最新一天 → 資料永久遺失。
  - 00982A、00992A 缺 6/18：群益要求輸入「下一個交易日」，算成 6/19（端午）後
    網站跳錯誤視窗擋住下載，爬蟲點擊逾時當掉。
以後每年只要更新這一個檔案。

註：run_update.ps1 另有一份 PowerShell 版假日表（排程層的休市守門），
    更新年度假日時兩邊都要改。
"""
from datetime import date, timedelta

# 2026 年台股休市日（僅列平日；週末本來就跳過）
# 來源：臺灣證券交易所 https://www.twse.com.tw/zh/trading/holiday.html
# 這是全專案唯一來源：Python 端直接 import；PowerShell 端讀 holidays.json（由本檔匯出）。
TW_HOLIDAY_REASONS = {
    date(2026, 1, 1): "元旦",
    date(2026, 2, 12): "春節(封關結算)",
    date(2026, 2, 13): "春節(封關結算)",
    date(2026, 2, 16): "春節",
    date(2026, 2, 17): "春節",
    date(2026, 2, 18): "春節",
    date(2026, 2, 19): "春節",
    date(2026, 2, 20): "春節",
    date(2026, 2, 27): "和平紀念日補假",
    date(2026, 4, 3): "兒童節補假",
    date(2026, 4, 6): "清明節補假",
    date(2026, 5, 1): "勞動節",
    date(2026, 6, 19): "端午節",
    date(2026, 7, 10): "颱風假(臨時休市)",
    date(2026, 9, 25): "中秋節",
    date(2026, 9, 28): "教師節",
    date(2026, 10, 9): "國慶日補假",
    date(2026, 10, 26): "光復節補假",
    date(2026, 12, 25): "行憲紀念日",
}

# 各爬蟲以 `d in TW_MARKET_HOLIDAYS` 判斷，維持原本用法不變
TW_MARKET_HOLIDAYS = set(TW_HOLIDAY_REASONS)


def is_trading_day(d):
    return d.weekday() < 5 and d not in TW_MARKET_HOLIDAYS


def prev_trading_day(d):
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d):
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def last_trading_day(d=None):
    """d（預設今天）當天或之前最近的一個交易日。d 本身是交易日就回傳 d。
    與 prev_trading_day 的差別：prev 一定往前跳一天，這個不會。"""
    d = d or date.today()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def export_json(path="holidays.json"):
    """匯出 {YYYY-MM-DD: 原因} 給 PowerShell 端（run_update.ps1）讀取，
    避免假日表在 Python 與 PowerShell 各留一份、更新時漏改其中一邊。"""
    import json
    data = {d.isoformat(): r for d, r in sorted(TW_HOLIDAY_REASONS.items())}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


if __name__ == "__main__":
    d = export_json()
    print(f"holidays.json exported: {len(d)} days")
