# -*- coding: utf-8 -*-
"""
check_data_freshness.py — 資料過期警報（看門狗）。

若最新持股資料日落後「最後一個交易日」，主動發 Telegram 警告。

緣由：2026-09-11 Windows Update 半夜強制重開機後停在登入畫面，而排程原設為
      「需使用者登入才執行」，導致當天 7 次觸發全部落空 —— 沒抓資料、沒發訊息、
      週報也沒發，直到三天後才被發現。此腳本讓這類環境性失敗「當天就被察覺」。

判斷邏輯：
  - 台股 ETF 為當日揭露：最新 dataDate 應等於最後一個交易日。
  - 海外 ETF（OVERSEAS）為 T+1，允許落後一個交易日，故分開判斷、門檻放寬。

用法：
    python check_data_freshness.py          # 有問題才發 Telegram
    python check_data_freshness.py --force  # 一律發（測試用）
"""
import glob
import json
import os
import sys
from datetime import date, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from daily_digest import send_telegram, TW_ETFS, OVERSEAS_ETFS, SITE_URL  # noqa: E402

# 平日休市日（與 run_update.ps1 的表一致；每年初更新）
TW_HOLIDAYS = {
    "2026-01-01", "2026-02-12", "2026-02-13", "2026-02-16", "2026-02-17",
    "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-27", "2026-04-03",
    "2026-04-06", "2026-05-01", "2026-06-19", "2026-07-10", "2026-09-25",
    "2026-09-28", "2026-10-09", "2026-10-26", "2026-12-25",
}


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in TW_HOLIDAYS


def last_trading_day(today=None):
    d = today or date.today()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def prev_trading_day(d):
    c = d - timedelta(days=1)
    while not is_trading_day(c):
        c -= timedelta(days=1)
    return c


def _data_date(code):
    path = f"data_{code}.json"
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8")).get("meta", {}).get("dataDate")
    except Exception:
        return None


def main():
    force = "--force" in sys.argv
    ltd = last_trading_day()
    ltd_s = ltd.isoformat()
    ov_ok_s = prev_trading_day(ltd).isoformat()   # 海外 T+1 可接受的最舊日期

    tw_stale, ov_stale = [], []
    for code, name in TW_ETFS:
        dd = _data_date(code)
        if dd is None or dd < ltd_s:
            tw_stale.append((code, name, dd or "無資料"))
    for code, name in OVERSEAS_ETFS:
        dd = _data_date(code)
        if dd is None or dd < ov_ok_s:
            ov_stale.append((code, name, dd or "無資料"))

    if not tw_stale and not ov_stale and not force:
        print(f"[freshness] OK — 台股皆已更新至最後交易日 {ltd_s}")
        return 0

    lines = [
        "⚠️ ETF 追蹤資料過期警報",
        f"📅 最後交易日應為：{ltd_s}",
        "",
    ]
    if tw_stale:
        lines.append(f"🔴 台股 {len(tw_stale)}/{len(TW_ETFS)} 檔未更新：")
        for code, name, dd in tw_stale[:15]:
            lines.append(f"  • {code} {name}　目前：{dd}")
    if ov_stale:
        lines.append(f"\n🟠 海外 {len(ov_stale)}/{len(OVERSEAS_ETFS)} 檔落後逾 T+1：")
        for code, name, dd in ov_stale:
            lines.append(f"  • {code} {name}　目前：{dd}")
    lines += [
        "",
        "可能原因：排程未執行（電腦關機／未登入／Windows Update 重開機）、",
        "或來源網站當日尚未揭露。",
        "補抓指令： python backfill_day.py <YYYY-MM-DD>",
        "",
        SITE_URL,
    ]
    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
