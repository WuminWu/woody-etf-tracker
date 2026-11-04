# -*- coding: utf-8 -*-
"""日報／週報引擎必須「同樣的輸入 → 同樣的輸出」。

背景（2026-10-14）：render_digest 的「共識升溫／退潮」走訪的是 set，而 Python 每次
啟動的字串雜湊都不同（PYTHONHASHSEED 隨機），變化幅度同分的個股順序因此每輪都在變，
取前 4 名時還會換人。10/13 資料 18:31 就齊了，之後 5 輪裡有 4 輪改寫了網站上的日報，
Telegram 發出的那份也可能和網站版不同。

本測試以不同雜湊種子在子行程各跑一次 build_digest（只讀不寫），比對輸出是否逐字相同。

用法（專案根目錄）：
    python tests/test_digest_determinism.py
"""
import glob
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SEEDS = ["0", "1", "2", "3", "4"]

# 子行程：import 會 chdir 到專案根目錄；build_digest 只讀不寫（不發 Telegram、不存檔）
CHILD = r"""
import json, logging, sys
logging.disable(logging.CRITICAL)
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, sys.argv[1])
import daily_digest as d
rd, msg, cnt, _ = d.build_digest(sys.argv[2], group=sys.argv[3])
print(json.dumps({"date": rd, "count": cnt, "msg": msg}, ensure_ascii=False))
"""


def latest_tw_date():
    """台股組最常見的最新資料日（大多數 ETF 都更新到的那天）。"""
    sys.path.insert(0, ROOT)
    import logging
    logging.disable(logging.CRITICAL)
    from daily_digest import TW_ETFS
    dates = []
    for code, _ in TW_ETFS:
        p = os.path.join(ROOT, f"data_{code}.json")
        if os.path.exists(p):
            dates.append(json.load(open(p, encoding="utf-8"))["meta"].get("dataDate", ""))
    return max(set(dates), key=dates.count) if dates else None


def run(seed, ref_date, group):
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    out = subprocess.run([sys.executable, "-c", CHILD, ROOT, ref_date, group],
                         capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"seed={seed} group={group} 失敗：\n{out.stderr[-1500:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_digest_is_deterministic():
    ref = latest_tw_date()
    assert ref, "找不到台股資料檔"
    if not glob.glob(os.path.join(ROOT, "snapshots", "????-??-??.json")):
        print("沒有 snapshots，略過")
        return
    for group in ("tw", "overseas"):
        results = {s: run(s, ref, group) for s in SEEDS}
        digests = {s: hashlib.md5(r["msg"].encode("utf-8")).hexdigest()[:10] for s, r in results.items()}
        if len(set(digests.values())) != 1:
            base = results[SEEDS[0]]["msg"].splitlines()
            for s in SEEDS[1:]:
                other = results[s]["msg"].splitlines()
                diff = [f"  seed0: {a}\n  seed{s}: {b}" for a, b in zip(base, other) if a != b]
                if diff:
                    print(f"[{group}] seed {s} 與 seed 0 不同：\n" + "\n".join(diff[:5]))
            raise AssertionError(f"[{group}] 同一份資料產生不同日報：{digests}")
        r = results[SEEDS[0]]
        print(f"[{group}] {r['date']}（{r['count']} 檔）× {len(SEEDS)} 個雜湊種子：輸出完全相同 {digests[SEEDS[0]]}")


if __name__ == "__main__":
    try:
        test_digest_is_deterministic()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
    print("OK")
