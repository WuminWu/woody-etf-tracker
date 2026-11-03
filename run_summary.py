# -*- coding: utf-8 -*-
"""
run_summary.py — 排程執行結果回報（由 run_update.ps1 在每輪 git push 之後呼叫）。

背景：某支爬蟲或 git 失敗時原本是靜默的，使用者只能事後從資料發現，
多次發生「以為有更新、其實沒有」。這支讀 run_update.ps1 寫的 logs/last_run_results.json，
有失敗就立刻發 Telegram。

注意（2026-10-14 修正）：
  - 本檔 9/16 寫好後，run_update.ps1 從來沒有呼叫它，所以失敗通知一直沒有生效。
  - 結果檔是 PowerShell `Set-Content -Encoding UTF8` 寫的，開頭有 BOM；用 utf-8 讀會
    JSONDecodeError，以前被 except 吞掉後只印「no results file; skip」。一律用 utf-8-sig 讀。

發送規則（避免一天被打擾 7 次）：
  - 有腳本（或 git pull/push）失敗，且「今天、這組失敗」還沒通報過 → 發一則
  - 資料是否過期交給 22:30 的看門狗（check_data_freshness.py），這裡不重複提醒
  - --daily：不論有無失敗，都發一則當日總結（手動檢查用）
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from notify import send_telegram, safe_print   # noqa: E402
from daily_digest import TW_ETFS, OVERSEAS_ETFS   # noqa: E402
from tw_calendar import prev_trading_day   # noqa: E402

RESULTS = os.path.join("logs", "last_run_results.json")
MARKER = os.path.join("logs", "last_summary_sent.txt")


def load_results(path=RESULTS):
    """讀 run_update.ps1 的結果檔；回傳 list（讀不到回 None）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:   # PowerShell 寫的 UTF-8 帶 BOM
            results = json.load(f)
    except FileNotFoundError:
        return None
    if isinstance(results, dict):      # ConvertTo-Json 只有一筆時不會包成陣列
        results = [results]
    return results


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


HINTS = {
    "git push": "網站不會更新，請檢查 git 認證／網路",
    "git pull": "多半是工作目錄有未提交的修改",
    "上一輪未完成": "可能是電腦重開機、睡眠或程式當掉",
    "上一輪卡住": "多半是 git push 在等登入視窗，或某支爬蟲卡住",
    "時間不足": "爬蟲階段超過時間預算，剩下的留給下一輪",
}


def describe_failure(r):
    """一筆失敗的說明：名稱、結束碼與秒數；逾時、上一輪未完成等附上原因。"""
    what = r.get("script", "?")
    if what.endswith(".py") or what.startswith("git "):
        head = f"{what}（exit={r.get('exit')}，{r.get('sec', '?')}s）"
    else:
        head = what
    hint = HINTS.get(what)
    if what == "git pull" and r.get("exit") == 124:
        hint = None          # 逾時多半是網路，不是工作目錄的問題
    notes = [n for n in (r.get("note"), hint) if n]
    return head + (f"：{'；'.join(notes)}" if notes else "")


def build_message(results, now, failed):
    today = now.strftime("%Y-%m-%d")
    ok_funds, late_funds = _fund_status(today)
    scripts = [r for r in results if str(r.get("script", "")).endswith(".py")]
    lines = [
        f"{'🛠️ ETF 更新執行異常' if failed else '✅ ETF 每日更新完成'}",
        f"📅 {today} {now.strftime('%H:%M')}",
        f"📦 腳本：{len(scripts) - sum(1 for r in failed if r in scripts)}/{len(scripts)} 成功",
        f"📊 基金：{len(ok_funds)}/{len(ok_funds) + len(late_funds)} 已更新至最新交易日",
    ]
    if failed:
        lines.append("\n❌ 失敗：")
        for r in failed:
            lines.append("  • " + describe_failure(r))
        lines.append("下一輪排程會自動重試；細節見 logs/run_<日期>.log")
    if late_funds:
        lines.append("\n⏳ 尚未更新：")
        for code, name, dd in late_funds[:10]:
            lines.append(f"  • {code} {name}　目前：{dd}")
    slow = sorted(scripts, key=lambda r: -float(r.get("sec", 0) or 0))[:3]
    if slow:
        lines.append("\n⏱️ 最耗時：" + "、".join(
            f"{r['script'].replace('check_and_update_', '').replace('.py', '')} {r.get('sec')}s" for r in slow))
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    daily = "--daily" in argv
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    results = load_results()
    if results is None:
        print("no results file; skip")
        return 0

    failed = [r for r in results if int(r.get("exit", 0) or 0) != 0]
    key = today + "|" + ",".join(sorted(r["script"] for r in failed))
    if failed and _read_marker() == key and not daily:
        print("same failures already reported today; skip")
        return 0
    if not failed and not daily:
        print("all scripts OK; nothing to report")
        return 0

    msg = build_message(results, now, failed)
    sent = send_telegram(msg)          # 先發送再印：印不出 emoji 也不會擋住通知
    safe_print(msg)
    if sent and failed:
        _write_marker(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
