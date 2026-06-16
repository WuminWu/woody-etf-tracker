"""
backfill_digests.py
-------------------
用既有的 snapshots/{date}.json 回填過去每天的「主動 ETF 經理人都在買什麼」分析文字，
寫入 digests.json（{date: text}），供網站「每日分析」分頁切換日期回顧。

只需在首次導入此功能時跑一次；之後每日由 daily_digest.py 自動累積當天。
重跑安全：會以目前的分析邏輯重新產生並覆蓋所有日期。

用法：
    python backfill_digests.py
"""

import glob
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from daily_digest import (
    render_digest, find_prev_snapshot, save_digest, TW_ETFS,
)

TW_CODES = {c for c, _ in TW_ETFS}


def main():
    files = sorted(glob.glob("snapshots/????-??-??.json"))
    if not files:
        print("沒有 snapshots 檔案，無法回填。")
        return

    written = 0
    for f in files:
        date = os.path.basename(f)[:10]
        snap = json.loads(open(f, encoding="utf-8").read())

        etf_holdings, updated = {}, []
        for etf_id, blk in snap.items():
            if etf_id in TW_CODES:
                etf_holdings[etf_id] = blk.get("holdings", [])
                updated.append(etf_id)
        if not updated:
            continue

        prev_snap = find_prev_snapshot(date)
        message, count = render_digest(date, etf_holdings, updated, prev_snap)
        save_digest(date, message)
        written += 1
        print(f"  {date}: {count} 檔 ETF")

    print(f"完成：回填 {written} 天的分析到 digests.json")


if __name__ == "__main__":
    main()
