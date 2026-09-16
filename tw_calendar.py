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
TW_MARKET_HOLIDAYS = {
    date(2026, 1, 1),    # 元旦
    date(2026, 2, 12), date(2026, 2, 13),                       # 春節封關結算
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20),                       # 春節
    date(2026, 2, 27),   # 和平紀念日補假
    date(2026, 4, 3),    # 兒童節補假
    date(2026, 4, 6),    # 清明節補假
    date(2026, 5, 1),    # 勞動節
    date(2026, 6, 19),   # 端午節
    date(2026, 7, 10),   # 颱風假（臨時休市）
    date(2026, 9, 25),   # 中秋節
    date(2026, 9, 28),   # 教師節
    date(2026, 10, 9),   # 國慶日補假
    date(2026, 10, 26),  # 光復節補假
    date(2026, 12, 25),  # 行憲紀念日
}


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
