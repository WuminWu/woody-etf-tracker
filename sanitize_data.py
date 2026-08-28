# -*- coding: utf-8 -*-
"""
sanitize_data.py — 出貨前把 data_*.json / data_index.json 內的 NaN / Infinity 清乾淨。

背景：Python 的 json.dump 預設會把 float('nan') 寫成字面 `NaN`，這是**非法 JSON**，
瀏覽器 fetch().json() 一遇到就整個解析失敗 → 前端 JS 中斷、網頁打不開。
任何爬蟲的市值/價格計算若偶發 NaN（例如 yfinance 回傳空值又落到 fallback），
都可能污染輸出。此腳本作為管線最後一道防線，在 git commit 前執行：

  - meta.prevTotalMarketCap 為 NaN → 用「前一交易日 snapshot 的 totalMarketCap」回填，
    再退而用當前 totalMarketCap。
  - 其他任何 NaN/Inf 數值 → 一律改為 0（保守，寧可顯示 0 也不要讓整頁掛掉）。

只在真的有改動時才覆寫檔案，避免無謂的 git diff。
"""
import json
import glob
import math
import os
from datetime import datetime, timedelta


def _prev_trading_day(dstr):
    try:
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
    except Exception:
        return None
    k = 1
    while k < 15:
        c = d - timedelta(days=k)
        if c.weekday() < 5:
            return c.strftime("%Y-%m-%d")
        k += 1
    return None


def _is_bad(v):
    return isinstance(v, float) and (math.isnan(v) or math.isinf(v))


def _snapshot_market_cap(code, data_date):
    ptd = _prev_trading_day(data_date) if data_date else None
    if not ptd:
        return None
    snap = os.path.join("snapshots", f"{ptd}.json")
    if not os.path.exists(snap):
        return None
    try:
        sd = json.load(open(snap, encoding="utf-8"))
        v = sd.get(code, {}).get("meta", {}).get("totalMarketCap")
        return None if _is_bad(v) else v
    except Exception:
        return None


def _walk_fix(obj, ctx):
    """遞迴把 NaN/Inf 換成安全值；回傳是否有改動。ctx 提供 prevTotalMarketCap 的回填來源。"""
    changed = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_bad(v):
                if k == "prevTotalMarketCap":
                    obj[k] = ctx.get("prevMC") if ctx.get("prevMC") is not None else ctx.get("curMC", 0.0)
                else:
                    obj[k] = 0.0
                changed = True
            elif isinstance(v, (dict, list)):
                changed = _walk_fix(v, ctx) or changed
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if _is_bad(v):
                obj[i] = 0.0
                changed = True
            elif isinstance(v, (dict, list)):
                changed = _walk_fix(v, ctx) or changed
    return changed


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    fixed = []
    for f in sorted(glob.glob("data_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        meta = d.get("meta", {}) if isinstance(d, dict) else {}
        code = os.path.basename(f)[5:-5]
        cur_mc = meta.get("totalMarketCap")
        ctx = {
            "curMC": cur_mc if not _is_bad(cur_mc) else 0.0,
            "prevMC": _snapshot_market_cap(code, meta.get("dataDate", "")),
        }
        if _walk_fix(d, ctx):
            json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
            fixed.append(code)
    if fixed:
        print(f"[sanitize] 清除 NaN/Inf：{', '.join(fixed)}")
    else:
        print("[sanitize] 無 NaN/Inf，資料乾淨。")


if __name__ == "__main__":
    main()
