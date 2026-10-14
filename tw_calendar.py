# -*- coding: utf-8 -*-
"""
tw_calendar.py — 台股交易日曆（全專案單一來源）。

各爬蟲原本各自抄一份休市日表，17/19 支缺了 12 天，已實際造成資料遺失：
  - 00988A 缺 4/30、6/18、7/9（都是休市日前一天）：T+1 爬蟲把休市日當成前一交易日，
    永遠等不到那天的檔案，而統一的網站只保留最新一天 → 資料永久遺失。
  - 00982A、00992A 缺 6/18：群益要求輸入「下一個交易日」，算成 6/19（端午）後
    網站跳錯誤視窗擋住下載，爬蟲點擊逾時當掉。

休市日來源（2026-10-14 起）：
  1. 下面手寫的 TW_HOLIDAY_REASONS（優先）：2026 年全年，以及颱風假等臨時休市。
  2. tw_holidays_twse.json：sync_tw_holidays.py 每天從證交所匯入「當年＋次年」的休市日，
     證交所一公布次年行事曆就自動生效，不用再等人手動補（2027 年截至 10/14 尚未公布）。
  看門狗會在跨年前 30 天檢查次年是否已涵蓋，沒有就發 Telegram 提醒。
PowerShell 端（run_update.ps1）讀本檔匯出的 holidays.json，不另存一份。
"""
import json
import os
from datetime import date, timedelta

# 2026 年台股休市日（僅列平日；週末本來就跳過）
# 來源：臺灣證券交易所 https://www.twse.com.tw/zh/trading/holiday.html
# 颱風假等臨時休市證交所行事曆不會列，請直接加在這裡。
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
    date(2026, 10, 23): "颱風假(臨時休市)",
    date(2026, 10, 26): "光復節補假",
    date(2026, 12, 25): "行憲紀念日",
}

# 上面手寫清單完整涵蓋的年份
MANUAL_YEARS = {2026}

# 證交所匯入的休市日（sync_tw_holidays.py 產生；手寫清單優先）
TWSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tw_holidays_twse.json")


def _load_twse(path=TWSE_FILE):
    """回傳 (已匯入的年份 set, {date: 名稱})；檔案不存在或壞掉時視為沒有。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        days = {date.fromisoformat(k): v for k, v in data.get("holidays", {}).items()}
        return set(int(y) for y in data.get("years", [])), days
    except (FileNotFoundError, ValueError, TypeError):
        return set(), {}


_TWSE_YEARS, _TWSE_DAYS = _load_twse()
for _d, _reason in _TWSE_DAYS.items():
    TW_HOLIDAY_REASONS.setdefault(_d, _reason)

# 各爬蟲以 `d in TW_MARKET_HOLIDAYS` 判斷，維持原本用法不變
TW_MARKET_HOLIDAYS = set(TW_HOLIDAY_REASONS)


def covered_years():
    """休市日表完整涵蓋的年份（手寫＋證交所已公布並匯入）。"""
    return MANUAL_YEARS | _TWSE_YEARS


def uncovered_year_ahead(today=None, horizon_days=30):
    """today 起 horizon_days 天內會用到、但休市日表尚未涵蓋的年份；都涵蓋則回 None。
    沒涵蓋的年份會把元旦、春節當成交易日：T+1 爬蟲等不到資料、群益會輸入休市日而當掉。"""
    today = today or date.today()
    for y in range(today.year, (today + timedelta(days=horizon_days)).year + 1):
        if y not in covered_years():
            return y
    return None


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
