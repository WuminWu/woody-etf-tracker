# -*- coding: utf-8 -*-
"""
run_summary.py — 每日執行摘要（由 run_update.ps1 在最後呼叫）。

背景：某支爬蟲失敗時原本是靜默的，使用者只能事後從資料發現，
多次發生「以為有更新、其實沒有」。這支會在每天最後一輪主動回報全貌。

發送規則（避免一天被打擾 7 次）：
  - 有腳本失敗，且該失敗組合今天還沒通報過 → 立刻通報
  - 當天最後一輪（21:00 之後）且今天還沒發過摘要 → 發一則每日總結
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from notify import send_telegram          # noqa: E402
from daily_digest import TW_ETFS, OVERSEAS_ETFS   # noqa: E402
from tw_calendar import prev_trading_day   # noqa: E402

RESULTS = os.path.join("logs", "last_run_results.json")
MARKER = os.path.join("logs", "last_summary_sent.txt")


def _read_marker():
    try:
        return open(MARKER, encoding="utf-8").read().strip()
    except Exception:
        return ""


def _write_marker(v):
    os.makedirs("logs", exist_ok=True)
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write(v)


def _fund_status(today):
    """回傳 (已更新清單, 落後清單)。海外組為 T+1，允許落後一個交易日。"""
    ok, late = [], []
    ov = {c for c, _ in OVERSEAS_ETFS}
    ok_ov_date = prev_trading_day(datetime.strptime(today, "%Y-%m-%d").date()).isoformat()
    for code, name in list(TW_ETFS) + list(OVERSEAS_ETFS):
        try:
            m = json.load(open(f"data_{code}.json", encoding="utf-8"))["meta"]
        except Exception:
            late.append((code, name, "無資料")); continue
        dd = m.get("dataDate", "")
        want = ok_ov_date if code in ov else today
        (ok if dd >= want else late).append((code, name, dd))
    return ok, late


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    try:
        results = json.load(open(RESULTS, encoding="utf-8"))
    except Exception:
        print("no results file; skip"); return 0
    if isinstance(results, dict):
        results = [results]

    failed = [r for r in results if int(r.get("exit", 0)) != 0]
    key = today + "|" + ",".join(sorted(r["script"] for r in failed))
    marker = _read_marker()
    is_last_round = now.hour >= 21
    already_summarised = marker.startswith(today) and marker.endswith("|done")

    if failed and marker != key:
        reason = "fail"
    elif is_last_round and not already_summarised:
        reason = "daily"
    else:
        print("nothing to report"); return 0

    ok_funds, late_funds = _fund_status(today)
    lines = [
        f"{'🛠️ ETF 更新執行異常' if failed else '✅ ETF 每日更新完成'}",
        f"📅 {today} {now.strftime('%H:%M')}",
        f"📦 腳本：{len(results) - len(failed)}/{len(results)} 成功",
        f"📊 基金：{len(ok_funds)}/{len(ok_funds) + len(late_funds)} 已更新至最新交易日",
    ]
    if failed:
        lines.append("\n❌ 失敗腳本：")
        for r in failed:
            lines.append(f"  • {r['script']}（exit={r.get('exit')}，{r.get('sec', '?')}s）")
    if late_funds:
        lines.append("\n⏳ 尚未更新：")
        for code, name, dd in late_funds[:10]:
            lines.append(f"  • {code} {name}　目前：{dd}")
    slow = sorted(results, key=lambda r: -float(r.get("sec", 0)))[:3]
    lines.append("\n⏱️ 最耗時：" + "、".join(f"{r['script'].replace('check_and_update_', '').replace('.py', '')} {r.get('sec')}s" for r in slow))
    msg = "\n".join(lines)
    print(msg)
    if send_telegram(msg):
        _write_marker(key if reason == "fail" else today + "||done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
