# -*- coding: utf-8 -*-
"""Golden-master harness（重構安全網）：以完全確定性的假 yfinance 呼叫每支爬蟲的 generate_data_json
（--mode old）或 etf_core.build_data_json（--mode new），把結果存成 JSON 供逐鍵比對。

沙箱注意：每支爬蟲 import 時都會 os.chdir 到自己 __file__ 所在目錄，
所以必須把 .py 一起複製進沙箱，否則會寫到正式資料檔（已踩過一次）。
跑完會驗證正式目錄的 data_*.json 沒被動到。

用法（在專案根目錄）：
    python tests/golden_master.py --mode wired --out /tmp/now.json --sandbox /tmp/sb
    python tests/cmp_snapshots.py tests/reference_snapshot.json /tmp/now.json

reference_snapshot.json 是重構前 19 支各自 generate_data_json 的輸出，
之後改 etf_core 只要比對這份快照，就知道有沒有不小心動到數字。
--mode old 只在重構前可用（那些函式已被刪除）。
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import types

import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ap.add_argument("--out", required=True)
ap.add_argument("--sandbox", required=True)
ap.add_argument("--mode", choices=["old", "new", "wired"], required=True)
a = ap.parse_args()
ROOT = os.path.abspath(a.root)

# ---- 每檔設定（--mode new 用；之後原封不動搬進各爬蟲）----
CFGS = {
    "00400A": dict(manager="梁恩溢", ipo_date="2026-04-09", ipo_price=10.0),
    "00403A": dict(manager="統一投信", ipo_date="2026-05-12", ipo_price=10.0, has_asset_alloc=True),
    "00405A": dict(manager="高晧欣", ipo_date="2026-06-09", ipo_price=10.0),
    "00407A": dict(manager="趙偉志", ipo_date="2026-06-24", ipo_price=10.0),
    "00409A": dict(manager="胡家菱", ipo_date="2026-09-02", ipo_price=10.0),
    "00411A": dict(manager="郭智偉", ipo_date="2026-08-26", ipo_price=10.0, has_asset_alloc=True,
                   yf_suffix=".TWO"),   # 上櫃掛牌
    "00980A": dict(manager="游景德"),
    "00981A": dict(manager="陳釧瑤", has_asset_alloc=True),
    "00982A": dict(manager="陳沅易"),
    "00985A": dict(manager="林浩詳"),
    "00987A": dict(manager="魏永祥", aum_derive_check=True),
    "00988A": dict(manager="陳意婷", has_asset_alloc=True),
    "00990A": dict(manager="元大投信", ipo_date="2025-12-22", ipo_price=10.0),
    "00991A": dict(manager="呂宏宇"),
    "00992A": dict(manager="陳朝政"),
    "00993A": dict(manager="安聯投信", units_are_zhang=True, aum_yf_fallback=True,
                   aum_carry_prev=False, has_futures=True),
    "00995A": dict(manager="中信投信", units_are_zhang=True, aum_yf_fallback=True,
                   aum_carry_prev=False),
    "00996A": dict(manager="王仲良"),
    "00997A": dict(manager="吳承恕", ipo_date="2026-04-14", ipo_price=10.0),
}
codes = sorted(CFGS)

# ---- 記錄正式檔指紋，最後驗證沒被動過 ----
def fingerprint():
    fp = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "data_*.json"))):
        fp[os.path.basename(p)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return fp

BEFORE_FP = fingerprint()

# ---- 建沙箱：.py + holidays.json + 每檔最後 6 天 holdings + data_*.json ----
SB = os.path.abspath(a.sandbox)
if os.path.isdir(SB):
    shutil.rmtree(SB)
os.makedirs(os.path.join(SB, "holdings"))
for p in glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "holidays.json")):
    shutil.copy2(p, os.path.join(SB, os.path.basename(p)))
for c in codes:
    for f in sorted(glob.glob(os.path.join(ROOT, "holdings", f"{c}_holdings_*.json")))[-6:]:
        shutil.copy2(f, os.path.join(SB, "holdings", os.path.basename(f)))
    d = os.path.join(ROOT, f"data_{c}.json")
    if os.path.exists(d):
        shutil.copy2(d, os.path.join(SB, f"data_{c}.json"))

# ---- 假 yfinance（確定性）----
def _seed(sym):
    return int(hashlib.md5(sym.encode()).hexdigest()[:8], 16)

class _FakeTicker:
    def __init__(self, sym):
        self.sym = sym
    def history(self, period="1d", timeout=10, **kw):
        s = _seed(self.sym)
        n = 1 if period == "1d" else 40
        base = 10 + (s % 90000) / 1000.0
        closes = [round(base * (1 + ((s >> (i % 16)) % 100 - 50) / 5000.0), 4) for i in range(n)]
        return pd.DataFrame({"Close": closes},
                            index=pd.date_range("2026-01-02", periods=n, freq="D"))
    @property
    def info(self):
        return {"totalAssets": float(_seed(self.sym) % 900) * 1e8 / 9}

fake = types.ModuleType("yfinance")
fake.Ticker = _FakeTicker
sys.modules["yfinance"] = fake

# 阻斷 Sheets / Telegram 副作用
sh = types.ModuleType("sheets_helper")
sh.append_holdings_to_sheets = lambda *aa, **kk: None
sys.modules["sheets_helper"] = sh
nt = types.ModuleType("notify")
nt.send_telegram = lambda *aa, **kk: True
nt._split_message = lambda t, limit=3900: [t]
sys.modules["notify"] = nt

sys.path.insert(0, SB)          # 從沙箱 import，讓 __file__ 指向沙箱
os.chdir(SB)


def inputs_for(c):
    """今日/前一日持股 + 資料日期。太新的基金只有一天資料 -> 合成前一日。"""
    files = sorted(glob.glob(os.path.join("holdings", f"{c}_holdings_*.json")))
    today_f = files[-1]
    today = json.load(open(today_f, encoding="utf-8"))
    ddate = os.path.basename(today_f)[-15:-5]
    if len(files) >= 2:
        prev = json.load(open(files[-2], encoding="utf-8"))
    else:
        prev = [dict(h) for h in today]
        for i, h in enumerate(prev):
            h["shares"] = int(h.get("shares", 0) * (0.8 + (i % 7) * 0.05)) or 1000
        prev = prev[1:] + [dict(prev[0], code="9999", name="SOLD_OUT_TEST",
                               shares=123000, weight=0.5)]
    return today, prev, ddate


AUM = 12_345_678_900
UNITS_SHARES = 456_789_000
UNITS_ZHANG = 456_789

results = {}
if a.mode == "old":
    import importlib
    import inspect
    for c in codes:
        try:
            m = importlib.import_module(f"check_and_update_{c}")
            os.chdir(SB)        # 爬蟲 import 時會 chdir 到 __file__ 目錄（= 沙箱），保險再設一次
            today, prev, ddate = inputs_for(c)
            sig = inspect.signature(m.generate_data_json)
            kw = {}
            if "manager" in sig.parameters:
                kw["manager"] = "TEST_MGR"
            if "asset_alloc" in sig.parameters:
                kw["asset_alloc"] = None
            units = UNITS_ZHANG if "units_zhang" in sig.parameters else UNITS_SHARES
            w = m.generate_data_json(today, prev, ddate, AUM, units, **kw)
            w = json.loads(json.dumps(w, ensure_ascii=False, sort_keys=True))
            w.get("meta", {}).pop("lastUpdate", None)
            results[c] = w
        except Exception as e:
            import traceback
            results[c] = {"__error__": f"{type(e).__name__}: {e}",
                          "__tb__": traceback.format_exc()[-900:]}
elif a.mode == "wired":
    # 重構後：用爬蟲自己定義的 CFG（順帶驗證每支的設定填對了）
    import importlib
    from etf_core import build_data_json
    for c in codes:
        try:
            m = importlib.import_module(f"check_and_update_{c}")
            os.chdir(SB)
            cfg = m.CFG
            today, prev, ddate = inputs_for(c)
            kw = {}
            if cfg.units_are_zhang:
                kw["units"] = UNITS_ZHANG
                kw["manager"] = "TEST_MGR"
            else:
                kw["units"] = UNITS_SHARES
            if cfg.has_asset_alloc:
                kw["asset_alloc"] = None
            w = build_data_json(cfg, today, prev, ddate, aum_ntd=AUM, **kw)
            w = json.loads(json.dumps(w, ensure_ascii=False, sort_keys=True))
            w.get("meta", {}).pop("lastUpdate", None)
            results[c] = w
        except Exception as e:
            import traceback
            results[c] = {"__error__": f"{type(e).__name__}: {e}",
                          "__tb__": traceback.format_exc()[-900:]}
else:
    from etf_core import FundConfig, build_data_json
    for c in codes:
        try:
            cfg = FundConfig(code=c, **CFGS[c])
            today, prev, ddate = inputs_for(c)
            kw = {}
            if cfg.units_are_zhang:
                kw["units"] = UNITS_ZHANG
                kw["manager"] = "TEST_MGR"
            else:
                kw["units"] = UNITS_SHARES
            if cfg.has_asset_alloc:
                kw["asset_alloc"] = None
            w = build_data_json(cfg, today, prev, ddate, aum_ntd=AUM, **kw)
            w = json.loads(json.dumps(w, ensure_ascii=False, sort_keys=True))
            w.get("meta", {}).pop("lastUpdate", None)
            results[c] = w
        except Exception as e:
            import traceback
            results[c] = {"__error__": f"{type(e).__name__}: {e}",
                          "__tb__": traceback.format_exc()[-900:]}

os.chdir(ROOT)
json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
ok = sum(1 for v in results.values() if "__error__" not in v)
print(f"[{a.mode}] captured {ok}/{len(codes)} -> {a.out}")
for c, v in sorted(results.items()):
    if "__error__" in v:
        print(f"  {c} {v['__error__']}")
        print("   ", v.get("__tb__", "").replace("\n", "\n    ")[-500:])

changed = [k for k, v in fingerprint().items() if BEFORE_FP.get(k) != v]
if changed:
    print("!! 嚴重：正式資料檔被寫入 ->", changed)
    sys.exit(2)
print("OK: 正式 data_*.json 未被動到")
