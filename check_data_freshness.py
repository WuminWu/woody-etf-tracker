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
from datetime import date, datetime, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from daily_digest import send_telegram, TW_ETFS, OVERSEAS_ETFS, SITE_URL  # noqa: E402
from notify import safe_print   # noqa: E402

from tw_calendar import is_trading_day, prev_trading_day, last_trading_day   # 單一來源：休市日與交易日判斷


def _data_date(code):
    path = f"data_{code}.json"
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8")).get("meta", {}).get("dataDate")
    except Exception:
        return None


def stuck_run_note(now=None):
    """今天有一輪排程沒跑完（run_update.ps1 的執行中標記還在、而且超過 60 分鐘）就回傳說明。

    卡住的那一輪發不出失敗通知（2026-10-08 21:00 的 git push 在等登入視窗，掛了好幾天），
    最後一輪（21:00）出事時，下一個交易日之前只有看門狗看得到。"""
    try:
        m = json.load(open(os.path.join("logs", "run_in_progress.json"), encoding="utf-8-sig"))
        start = datetime.strptime(m["start"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""
    now = now or datetime.now()
    if start.date() != now.date() or (now - start).total_seconds() < 3600:
        return ""
    return (f"⏱️ {start:%H:%M} 開始的那一輪排程沒有跑完（多半是 git push 在等登入視窗，或某支爬蟲卡住）。"
            f"下一輪會結束它並重試；細節見 logs/run_{start:%Y%m%d}.log。")


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

    # 試算表對帳（不需憑證）：最近 30 天 holdings 有、試算表沒有，或標在休市日的資料。
    # 試算表是快照／歷史／週報的來源，缺一天就整條下游都少一天（00980A 7/30 曾因此永久缺漏）。
    gaps_text = ""
    try:
        from sheets_reconcile import find_gaps, format_gaps
        g = find_gaps(days=30)
        if g["missing"] or g["holiday"]:
            gaps_text = format_gaps({"missing": g["missing"], "extra": [], "holiday": g["holiday"]})
    except BaseException as e:      # find_gaps 讀不到 manifest 時會 SystemExit
        print(f"[freshness] 試算表對帳略過：{e}")

    # 跨年前 30 天：次年休市日還沒涵蓋就提醒（證交所公布後 sync_tw_holidays.py 會自動匯入）
    from tw_calendar import uncovered_year_ahead
    year_gap = uncovered_year_ahead(horizon_days=30)
    cal_only = False
    if year_gap:
        cal_note = (f"📅 {year_gap} 年台股休市日尚未匯入：證交所公布後排程會自動匯入"
                    f"（sync_tw_holidays.py）；若已公布仍未匯入，請手動執行 "
                    f"python sync_tw_holidays.py --force 或補進 tw_calendar.py。")
        cal_only = not gaps_text
        gaps_text = (gaps_text + "\n\n" if gaps_text else "") + cal_note

    stuck = stuck_run_note()

    if not tw_stale and not ov_stale and not gaps_text and not stuck and not force:
        print(f"[freshness] OK — 台股皆已更新至最後交易日 {ltd_s}；最近 30 天試算表無缺漏")
        return 0

    lines = [
        "⚠️ ETF 追蹤資料過期警報" if (tw_stale or ov_stale)
        else "⏱️ ETF 排程有一輪沒有跑完" if stuck
        else "📅 ETF 休市日表提醒" if cal_only else "🧾 ETF 試算表對帳發現缺漏",
        f"📅 最後交易日應為：{ltd_s}",
        "",
    ]
    if stuck:
        lines += [stuck, ""]
    if tw_stale:
        lines.append(f"🔴 台股 {len(tw_stale)}/{len(TW_ETFS)} 檔未更新：")
        for code, name, dd in tw_stale[:15]:
            lines.append(f"  • {code} {name}　目前：{dd}")
    if ov_stale:
        lines.append(f"\n🟠 海外 {len(ov_stale)}/{len(OVERSEAS_ETFS)} 檔落後逾 T+1：")
        for code, name, dd in ov_stale:
            lines.append(f"  • {code} {name}　目前：{dd}")
    if gaps_text:
        lines += ["", gaps_text]
    lines += [
        "",
        "可能原因：排程未執行（電腦關機／未登入／Windows Update 重開機）、",
        "或來源網站當日尚未揭露。",
        "補抓指令： python backfill_day.py <YYYY-MM-DD>",
        "",
        SITE_URL,
    ]
    msg = "\n".join(lines)
    send_telegram(msg)       # 先發送再印：以前先 print，cp950 主控台一印 emoji 就當掉、警報發不出去
    safe_print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
