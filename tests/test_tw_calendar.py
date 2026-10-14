# -*- coding: utf-8 -*-
"""休市日：證交所匯入規則必須與手寫的 2026 年清單一致；跨年前會偵測到尚未涵蓋的年份。

用法（專案根目錄）：python tests/test_tw_calendar.py
"""
import json
import os
import sys
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

import tw_calendar as cal          # noqa: E402
from sync_tw_holidays import parse  # noqa: E402

TYPHOON_2026 = {date(2026, 7, 10)}   # 臨時休市，證交所行事曆不會列


def test_twse_parser_matches_manual_2026():
    doc = json.load(open(os.path.join(ROOT, "tests", "fixtures", "twse_holidays_2026.json"), encoding="utf-8"))
    parsed = set(parse(doc))
    manual = {d for d in cal.TW_HOLIDAY_REASONS if d.year == 2026 and d not in TYPHOON_2026}
    assert parsed == manual, f"只在證交所：{sorted(parsed - manual)}；只在手寫：{sorted(manual - parsed)}"
    # 不能把「開始交易日／最後交易日」當休市
    assert date(2026, 1, 2) not in parsed and date(2026, 2, 11) not in parsed
    # 封關（無交易、僅結算交割）要算休市
    assert date(2026, 2, 12) in parsed and date(2026, 2, 13) in parsed


def test_unpublished_year_returns_none():
    assert parse({"data": [], "total": 0}) is None


def test_uncovered_year_ahead():
    covered = cal.covered_years()
    assert 2026 in covered
    assert cal.uncovered_year_ahead(date(2026, 10, 14), 30) is None
    nxt = cal.uncovered_year_ahead(date(2026, 12, 15), 30)
    assert nxt == (None if 2027 in covered else 2027)


def test_trading_days_2026():
    assert not cal.is_trading_day(date(2026, 1, 1))      # 元旦
    assert not cal.is_trading_day(date(2026, 7, 10))     # 颱風假（手寫）
    assert cal.is_trading_day(date(2026, 10, 14))
    assert cal.next_trading_day(date(2026, 10, 23)) == date(2026, 10, 27)   # 10/26 光復節補假
    assert cal.prev_trading_day(date(2026, 2, 23)) == date(2026, 2, 11)     # 春節＋封關


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("OK")
