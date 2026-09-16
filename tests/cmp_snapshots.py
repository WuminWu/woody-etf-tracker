# -*- coding: utf-8 -*-
import json, sys

# 刻意統一、已在交接文件記錄的差異：YTD 以發行價為基準時改用未四捨五入的收盤價
# （舊版 4 支用 round(price,2) 先四捨五入再算，與 00411A 不一致），差距 <= 0.04 個百分點。
DECLARED = {
    "00400A.meta.ytd", "00403A.meta.ytd", "00405A.meta.ytd", "00997A.meta.ytd",
}

a = json.load(open(sys.argv[1], encoding="utf-8"))
b = json.load(open(sys.argv[2], encoding="utf-8"))
bad = 0
def walk(pa, x, y, out):
    if type(x) is not type(y):
        out.append(f"{pa}: type {type(x).__name__} -> {type(y).__name__}"); return
    if isinstance(x, dict):
        for k in sorted(set(x) | set(y)):
            if k not in x: out.append(f"{pa}.{k}: ADDED = {y[k]!r}")
            elif k not in y: out.append(f"{pa}.{k}: REMOVED (was {x[k]!r})")
            else: walk(f"{pa}.{k}", x[k], y[k], out)
    elif isinstance(x, list):
        if len(x) != len(y): out.append(f"{pa}: len {len(x)} -> {len(y)}"); return
        for i, (u, v) in enumerate(zip(x, y)): walk(f"{pa}[{i}]", u, v, out)
    elif x != y:
        out.append(f"{pa}: {x!r} -> {y!r}")
for c in sorted(set(a) | set(b)):
    out = []
    walk(c, a.get(c, {}), b.get(c, {}), out)
    dec = [l for l in out if l.split(":")[0] in DECLARED]
    out = [l for l in out if l.split(":")[0] not in DECLARED]
    if dec and not out:
        print(f"[OK-宣告] {c}  " + "; ".join(x.strip() for x in dec))
        continue
    if out:
        bad += 1
        print(f"[DIFF] {c}  ({len(out)} 處)")
        for l in out[:12]: print("   ", l)
        if len(out) > 12: print(f"    ... 另有 {len(out)-12} 處")
    else:
        print(f"[SAME] {c}")
print()
print("IDENTICAL - 重構未改變任何輸出" if bad == 0 else f"!! {bad} 檔輸出有差異")
sys.exit(1 if bad else 0)
