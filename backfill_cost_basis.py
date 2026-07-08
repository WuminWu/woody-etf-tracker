"""
backfill_cost_basis.py
----------------------
一次性：用 snapshots/（2026-04-22 起）依日期順序重放持股異動，重建各 ETF 個股成本價。

- 4/22 之後才「新建倉」的個股 → 成本可完整重建
- 4/22 前就持有的 → 起始成本不可考，維持無紀錄
- 海外 ETF（00988A/00997A）只從 2026-06-23 起回放：之前 snapshot 的海外股價
  為當地幣別（未換台幣），會污染成本

重跑安全：從零重建整份 cost_basis.json 後覆蓋。
用法：python backfill_cost_basis.py
"""

import glob
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from update_cost_basis import apply_day, save_cost_basis, COST_FILE

# 海外 ETF：此日之前 snapshot 內的股價/金額為當地幣別，跳過
OVERSEAS_FX_SAFE = {"00988A": "2026-06-23", "00997A": "2026-06-23"}


def main():
    cb = {"_lastProcessed": {}}
    last = cb["_lastProcessed"]
    files = sorted(glob.glob("snapshots/????-??-??.json"))
    if not files:
        print("沒有 snapshots，無法回填。")
        return

    for f in files:
        date_str = os.path.basename(f)[:10]
        try:
            snap = json.loads(open(f, encoding="utf-8").read())
        except Exception:
            continue
        for etf, blk in snap.items():
            if date_str < OVERSEAS_FX_SAFE.get(etf, ""):
                continue
            apply_day(cb, etf, blk.get("holdings", []), blk.get("meta", {}), date_str)
            last[etf] = date_str

    # 移除空的 ETF 條目（如已停止追蹤的 00404A）
    for e in [e for e, v in cb.items() if e != "_lastProcessed" and not v]:
        del cb[e]; last.pop(e, None)

    save_cost_basis(cb)
    tracked = {e: len(v) for e, v in cb.items() if e != "_lastProcessed"}
    print(f"回填完成（{files[0][10:20]} ~ {files[-1][10:20]}），寫入 {COST_FILE}")
    for e, n in sorted(tracked.items()):
        print(f"  {e}: 追蹤 {n} 檔（處理至 {last.get(e)}）")


if __name__ == "__main__":
    main()
