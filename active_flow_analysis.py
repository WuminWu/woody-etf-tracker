# -*- coding: utf-8 -*-
"""
active_flow_analysis.py
-----------------------
研究用：把「經理人主動加碼」從「申購資金被迫買進」裡拆出來，並回測它當選股訊號的效果。
只讀 snapshots/*.json，不連網、不寫試算表、不發通知。

拆解方式（每檔 ETF、每個交易日、每檔持股）：
  g          = 今日基金單位數 / 前一份快照單位數 − 1          （申購/贖回造成的規模變化）
  被動預期股數 = prevShares × (1 + g)                         （照比例放大的話應該有的股數）
  主動股數     = shares − 被動預期股數
  主動比例     = 主動股數 / prevShares

輸出三段：
  [1] 對齊測試：每天持股股數變化率的中位數 vs g —— 經理人是不是「照比例」買？
  [2] 現金測試：單位大增的日子，股票權重合計（≈ 1 − 現金）有沒有下降 —— 資金是不是先放現金？
  [3] 訊號回測：主動加碼、單純看 diffShares>0、新建倉、多家共識 等事件，
      資料公布隔天收盤進場，之後 5/10/20 個交易日相對「所有主動 ETF 台股持股等權平均」的超額報酬。

  [4] 主動加碼依 ETF 拆開。
  [5] 多日累積（同一 ETF 在 W 份快照內多天主動買進）＋排除追高（事件前 L 日超額漲幅分組），
      並附「所有持股在同樣前段漲幅下」的對照組，以及前後半段穩定度。

價格：取當天任何一檔 ETF 快照裡該股的 price（只有台股代號）。
     某檔股票若當天已無任何 ETF 持有就沒有價格，該事件會被略過，覆蓋率另外列出。
報酬未含股利，除息會讓報酬略低估。

用法：
  python active_flow_analysis.py                 # 預設門檻
  python active_flow_analysis.py --min-pct 2 --min-amt 30000000
  python active_flow_analysis.py --csv out.csv   # 另存逐筆事件
"""

import argparse
import csv
import glob
import json
import math
import os
import re
import statistics as st
import sys
from collections import defaultdict

os.chdir(os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TW_CODE = re.compile(r"^\d{4,6}[A-Z]?$")
HORIZONS = (5, 10, 20)


# ----------------------------------------------------------------- 載入資料
def load():
    dates = sorted(os.path.basename(f)[:-5] for f in glob.glob("snapshots/????-??-??.json"))
    snaps = {}
    for d in dates:
        with open(f"snapshots/{d}.json", encoding="utf-8") as f:
            snaps[d] = json.load(f)
    group = {}
    try:
        with open("etfs.json", encoding="utf-8") as f:
            group = {e["code"]: e.get("group", "tw") for e in json.load(f)}
    except Exception:
        pass
    return dates, snaps, group


def build_prices(dates, snaps):
    """price[date][code]：當天任何 ETF 快照裡的台股價格。"""
    price = {}
    for d in dates:
        p = {}
        for etf in snaps[d].values():
            for h in etf.get("holdings", []):
                c = str(h.get("code", "")).strip()
                v = h.get("price")
                if TW_CODE.match(c) and v and v > 0 and c not in p:
                    p[c] = float(v)
        price[d] = p
    return price


# ----------------------------------------------------------------- 拆解
def decompose(dates, snaps, baseline="units"):
    """
    baseline="units"  ：被動預期用基金單位數成長 g（假設經理人照比例放大）
    baseline="median" ：被動預期用當天所有持股股數變化率的中位數（不照比例買的 ETF 中位數≈0）

    逐 ETF、逐日計算 g 與每檔持股的主動股數。
    回傳 rows（每檔持股一列）與 days（每 ETF 每日一列的摘要）。
    """
    rows, days = [], []
    last = {}   # etf -> (date, units, {code: shares}, weightSum)
    for d in dates:
        for code_etf, etf in snaps[d].items():
            meta = etf.get("meta") or {}
            hold = etf.get("holdings") or []
            units = meta.get("totalShares")
            wsum = sum((h.get("todayWeight") or 0) for h in hold)
            cur = {str(h.get("code")): h.get("shares") or 0 for h in hold}
            prev = last.get(code_etf)
            last[code_etf] = (d, units, cur, wsum)
            if not prev or not units or not prev[1]:
                continue
            pdate, punits, pshares, pwsum = prev
            # prevShares 要對得上前一份快照，否則中間缺資料，這一天不算
            both = [h for h in hold if (h.get("prevShares") or 0) > 0 and str(h.get("code")) in pshares]
            if not both:
                continue
            match = sum(1 for h in both if pshares[str(h["code"])] == h["prevShares"]) / len(both)
            if match < 0.8:
                continue
            g = units / punits - 1
            ratios = [(h.get("shares") or 0) / h["prevShares"] - 1 for h in hold if (h.get("prevShares") or 0) > 0]
            base = st.median(ratios) if (baseline == "median" and ratios) else g
            buys = sum(max(0.0, ((h.get("shares") or 0) - (h.get("prevShares") or 0)) * (h.get("price") or 0))
                       for h in hold)
            chg = []
            for h in hold:
                ps, s = h.get("prevShares") or 0, h.get("shares") or 0
                c = str(h.get("code"))
                if ps <= 0:
                    if s > 0:
                        rows.append(dict(date=d, etf=code_etf, code=c, name=h.get("name"),
                                         kind="new", g=g, ratio=None, active_pct=None,
                                         active_amt=s * (h.get("price") or 0),
                                         diff=s, wt=h.get("todayWeight") or 0, ywt=0,
                                         shares=s, ps=0, act=s, px=h.get("price") or 0,
                                         buy_share=(s * (h.get("price") or 0) / buys) if buys else 0))
                    continue
                r = s / ps - 1
                chg.append(r)
                passive = ps * (1 + base)
                act = s - passive
                rows.append(dict(date=d, etf=code_etf, code=c, name=h.get("name"),
                                 kind="held", g=g, ratio=r, active_pct=act / ps,
                                 active_amt=act * (h.get("price") or 0),
                                 diff=s - ps, wt=h.get("todayWeight") or 0, ywt=h.get("yestWeight") or 0,
                                 shares=s, ps=ps, act=act, px=h.get("price") or 0,
                                 buy_share=(max(0, s - ps) * (h.get("price") or 0) / buys) if buys else 0))
            if chg:
                days.append(dict(date=d, etf=code_etf, g=g, med=st.median(chg),
                                 zero=sum(1 for x in chg if x == 0) / len(chg),
                                 up=sum(1 for x in chg if x > 0) / len(chg),
                                 down=sum(1 for x in chg if x < 0) / len(chg),
                                 n=len(chg), wsum=wsum, pwsum=pwsum))
    return rows, days


# ----------------------------------------------------------------- 報表工具
def pct(x, nd=2):
    return "  n/a " if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}%"


def summarize(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    m, sd = st.mean(vals), st.stdev(vals)
    return dict(n=len(vals), mean=m, med=st.median(vals),
                hit=sum(1 for v in vals if v > 0) / len(vals),
                t=m / (sd / math.sqrt(len(vals))) if sd > 0 else float("nan"))


# ----------------------------------------------------------------- [1][2]
def report_alignment(days, group):
    print("=" * 78)
    print("[1] 對齊測試：經理人是不是照申購比例買進全部持股？")
    print("=" * 78)
    flow = [x for x in days if abs(x["g"]) >= 0.005]          # 單位數變化 ≥ 0.5% 的日子
    flat = [x for x in days if abs(x["g"]) < 0.0005]
    print(f"ETF-日 共 {len(days)} 筆；單位數變化 ≥0.5% 的 {len(flow)} 筆；幾乎沒變的 {len(flat)} 筆\n")

    def blk(title, xs):
        if not xs:
            return
        print(f"  {title}（{len(xs)} 筆）")
        print(f"    持股股數完全沒動的比例 中位數 {st.median(x['zero'] for x in xs):.0%}"
              f"，有加的 {st.median(x['up'] for x in xs):.0%}，有減的 {st.median(x['down'] for x in xs):.0%}")
        gap = [abs(x["med"] - x["g"]) for x in xs]
        print(f"    |股數變化中位數 − g| 中位數 {st.median(gap) * 100:.2f} 個百分點，"
              f"< 0.1pp 的比例 {sum(1 for v in gap if v < 0.001) / len(gap):.0%}")

    blk("申購/贖回明顯的日子", flow)
    blk("單位數幾乎沒變的日子", flat)

    print("\n  各 ETF：單位數變化 ≥0.5% 的日子裡，「持股中位數有跟著 g 動」的比例")
    print("  （|中位數−g| < g 的一半視為照比例；越高代表越像被動放大）")
    by = defaultdict(list)
    for x in flow:
        by[x["etf"]].append(x)
    for etf in sorted(by, key=lambda e: -len(by[e])):
        xs = by[etf]
        prop = sum(1 for x in xs if abs(x["med"] - x["g"]) < abs(x["g"]) / 2) / len(xs)
        z = st.median(x["zero"] for x in xs)
        print(f"    {etf} [{group.get(etf, '?'):8s}] 天數 {len(xs):3d}  照比例 {prop:4.0%}  "
              f"持股沒動比例中位數 {z:4.0%}")


def report_cash(days):
    print("\n" + "=" * 78)
    print("[2] 現金測試：申購資金進來當天，股票權重合計（≈ 1 − 現金）怎麼變？")
    print("=" * 78)
    buckets = [("大量贖回 g ≤ −1%", lambda g: g <= -0.01),
               ("小幅贖回 −1%<g≤−0.2%", lambda g: -0.01 < g <= -0.002),
               ("幾乎不變 |g|<0.2%", lambda g: abs(g) < 0.002),
               ("小幅申購 0.2%≤g<1%", lambda g: 0.002 <= g < 0.01),
               ("大量申購 1%≤g<5%", lambda g: 0.01 <= g < 0.05),
               ("爆量申購 g ≥ 5%", lambda g: g >= 0.05)]
    print(f"  {'區間':22s} {'筆數':>5s} {'股票權重變化(中位數)':>18s} {'權重下降比例':>10s}")
    for name, f in buckets:
        xs = [x["wsum"] - x["pwsum"] for x in days if f(x["g"]) and x["pwsum"] > 50]
        if xs:
            print(f"  {name:22s} {len(xs):5d} {st.median(xs):+15.2f} pp {sum(1 for v in xs if v < 0) / len(xs):10.0%}")
    print("  權重下降＝錢先放現金，之後幾天才買；那幾天的買進會被算成「主動」，要小心。")

    # 延遲部署：大量申購後第 1~3 份快照的股票權重變化
    by = defaultdict(list)
    for x in days:
        by[x["etf"]].append(x)
    lag = defaultdict(list)
    for xs in by.values():
        for i, x in enumerate(xs):
            if x["g"] >= 0.01 and x["pwsum"] > 50:
                for k in (1, 2, 3):
                    if i + k < len(xs):
                        y = xs[i + k]
                        lag[k].append(y["wsum"] - y["pwsum"])
    if lag:
        print("  大量申購（g≥1%）之後第 k 天的股票權重變化中位數："
              + "  ".join(f"第{k}天 {st.median(v):+.2f}pp" for k, v in sorted(lag.items())))


# ----------------------------------------------------------------- [3] 回測
def fwd_returns(dates, price, d, code):
    """資料日 d 公布 → d 的下一個交易日收盤進場，回傳 {h: 報酬}，與公布當晚到進場的跳空。"""
    i = dates.index(d)
    if i + 1 >= len(dates):
        return None, None
    e = dates[i + 1]
    pe = price[e].get(code)
    p0 = price[d].get(code)
    gap = (pe / p0 - 1) if (pe and p0) else None
    out = {}
    for h in HORIZONS:
        j = i + 1 + h
        if j < len(dates) and pe:
            px = price[dates[j]].get(code)
            out[h] = (px / pe - 1) if px else None
        else:
            out[h] = None
    return out, gap


def bench_returns(dates, price):
    """所有主動 ETF 台股持股等權平均報酬：bench[(entry_index, h)]。"""
    cache = {}

    def f(i, h):
        key = (i, h)
        if key not in cache:
            if i + h >= len(dates):
                cache[key] = None
            else:
                a, b = price[dates[i]], price[dates[i + h]]
                rs = [b[c] / a[c] - 1 for c in a if c in b]
                cache[key] = st.mean(rs) if rs else None
        return cache[key]
    return f


def report_backtest(dates, price, rows, args):
    print("\n" + "=" * 78)
    print("[3] 訊號回測：公布隔天收盤進場，相對「主動 ETF 台股持股等權平均」的超額報酬")
    print("=" * 78)
    print(f"  門檻：主動比例 ≥ {args.min_pct}%、主動金額 ≥ {args.min_amt / 1e6:.0f} 百萬元；"
          f"只算台股代號；同一天同一檔股票合併成一個事件\n")
    bench = bench_returns(dates, price)
    di = {d: i for i, d in enumerate(dates)}
    tw = [r for r in rows if TW_CODE.match(r["code"])]
    min_pct, min_amt = args.min_pct / 100, args.min_amt

    signals = {
        "A. 單看 diffShares>0（現在日報的做法）":
            lambda r: r["kind"] == "held" and r["diff"] > 0,
        "B. 被動買進（有加股數但主動比例 < 門檻）":
            lambda r: r["kind"] == "held" and r["diff"] > 0 and r["active_pct"] < min_pct,
        "C. 主動加碼（去掉申購資金後）":
            lambda r: r["kind"] == "held" and r["active_pct"] >= min_pct and r["active_amt"] >= min_amt,
        "D. 主動減碼":
            lambda r: r["kind"] == "held" and r["active_pct"] <= -min_pct and r["active_amt"] <= -min_amt,
        "H. 超配加碼（占當日買進金額比例 ≥ 原權重 3 倍）":
            lambda r: r["kind"] == "held" and r["diff"] > 0 and r["active_amt"] >= min_amt
            and r["buy_share"] * 100 >= 3 * max(r["ywt"], 0.1),
        "E. 新建倉":
            lambda r: r["kind"] == "new" and r["active_amt"] >= min_amt,
    }

    events = {}
    for name, f in signals.items():
        ev = defaultdict(set)
        for r in tw:
            if f(r):
                ev[(r["date"], r["code"])].add(r["etf"])
        events[name] = ev
    # 共識：同一天 ≥2 / ≥3 家主動加碼（C 或 E）
    ce = defaultdict(set)
    for k in ("C. 主動加碼（去掉申購資金後）", "E. 新建倉"):
        for key, etfs in events[k].items():
            ce[key] |= etfs
    events["F. 共識主動加碼（同日 ≥2 家，含新建倉）"] = {k: v for k, v in ce.items() if len(v) >= 2}
    events["G. 共識主動加碼（同日 ≥3 家，含新建倉）"] = {k: v for k, v in ce.items() if len(v) >= 3}

    out_rows = []
    hdr = f"  {'訊號':38s} {'事件':>5s} {'覆蓋':>4s} {'跳空':>7s}"
    for h in HORIZONS:
        hdr += f" | {h:>2d}日超額 勝率  t值"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for name, ev in events.items():
        per_h = {h: defaultdict(list) for h in HORIZONS}   # h -> entry_date -> [excess]
        gaps, n_ok = [], 0
        for (d, code), etfs in ev.items():
            r, gap = fwd_returns(dates, price, d, code)
            if r is None:
                continue
            i = di[d] + 1
            got = False
            for h in HORIZONS:
                b = bench(i, h)
                if r[h] is not None and b is not None:
                    per_h[h][d].append(r[h] - b)
                    got = True
            if got:
                n_ok += 1
                if gap is not None:
                    gaps.append(gap - (bench(di[d], 1) or 0))
            out_rows.append(dict(signal=name[:2].strip(". "), date=d, code=code, etfs=",".join(sorted(etfs)),
                                 gap=gap, **{f"r{h}": r[h] for h in HORIZONS}))
        line = f"  {name:38s} {len(ev):5d} {n_ok / len(ev) if ev else 0:4.0%} {pct(st.mean(gaps) if gaps else None, 2):>7s}"
        for h in HORIZONS:
            # 同一天的事件先平均成一個「當日組合」，再對各日做統計；
            # 持有期重疊會讓 t 值偏高，這裡用 t / sqrt(h) 粗略打折
            cohort = [st.mean(v) for v in per_h[h].values()]
            s = summarize(cohort)
            if s:
                line += f" | {pct(s['mean']):>8s} {s['hit']:4.0%} {s['t'] / math.sqrt(h):+4.1f}"
            else:
                line += f" | {'n/a':>8s} {'':4s} {'':4s}"
        print(line)
    print("\n  說明：超額＝個股報酬 − 同期所有主動 ETF 台股持股等權平均；勝率＝超額為正的「交易日組合」比例；")
    print("        t 值已除以 √持有天數 粗略修正重疊；|t|<2 基本上就是看不出差別。")
    print("        跳空＝資料日收盤到隔天收盤（你最早能進場的價位）已經跑掉的超額報酬。")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
        print(f"\n  逐筆事件已存到 {args.csv}")
    return events


def report_by_etf(dates, price, rows, args):
    """C 訊號依 ETF 拆開：哪些經理人的主動加碼比較準。"""
    print("\n" + "=" * 78)
    print("[4] 主動加碼（C）依 ETF 拆開：10 日超額報酬")
    print("=" * 78)
    bench = bench_returns(dates, price)
    di = {d: i for i, d in enumerate(dates)}
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if (TW_CODE.match(r["code"]) and r["kind"] == "held"
                and r["active_pct"] >= args.min_pct / 100 and r["active_amt"] >= args.min_amt):
            fr, _ = fwd_returns(dates, price, r["date"], r["code"])
            b = bench(di[r["date"]] + 1, 10)
            if fr and fr[10] is not None and b is not None:
                by[r["etf"]][r["date"]].append(fr[10] - b)
    res = []
    for etf, dd in by.items():
        s = summarize([st.mean(v) for v in dd.values()])
        if s and s["n"] >= 10:
            res.append((etf, s))
    for etf, s in sorted(res, key=lambda x: -x[1]["mean"]):
        print(f"  {etf}  交易日 {s['n']:3d}  平均 {pct(s['mean'])}  中位數 {pct(s['med'])}  "
              f"勝率 {s['hit']:4.0%}  t {s['t'] / math.sqrt(10):+.1f}")


# ----------------------------------------------------------------- [5] 多日累積＋排除追高
RUNUP_BUCKETS = [("前段跌 ≤−5%", -9, -0.05), ("小跌 −5%~0", -0.05, 0.0), ("小漲 0~+5%", 0.0, 0.05),
                 ("漲 +5%~+10%", 0.05, 0.10), ("大漲 >+10%", 0.10, 9)]


def runup_of(dates, price, bench, di, d, code, L):
    """資料日 d 往前 L 個交易日的超額報酬（決策當下已知的資訊）。"""
    i = di[d]
    if i - L < 0:
        return None
    a, b = price[dates[i - L]].get(code), price[d].get(code)
    bm = bench(i - L, L)
    if not a or not b or bm is None:
        return None
    return b / a - 1 - bm


def bucket_of(x):
    for name, lo, hi in RUNUP_BUCKETS:
        if lo < x <= hi:
            return name
    return None


def accumulation_events(rows, W, min_days, min_amt, min_built, cooldown):
    """
    多日累積：同一檔 ETF 對同一檔股票，在自己最近 W 份快照內
      - 有主動買進的天數 ≥ min_days（新建倉當天也算一天）
      - 主動買進累積金額 ≥ min_amt
      - 這 W 天主動買進的股數占目前部位 ≥ min_built（排除大部位上的零星加碼）
    條件第一次成立那天記一個事件；之後 cooldown 份快照內同一 (ETF, 股票) 不重複記。
    回傳 {(date, code): set(etf)}。
    """
    etf_dates = defaultdict(set)
    by = defaultdict(dict)
    for r in rows:
        if not TW_CODE.match(r["code"]):
            continue
        etf_dates[r["etf"]].add(r["date"])
        by[(r["etf"], r["code"])][r["date"]] = r
    etf_dates = {e: sorted(v) for e, v in etf_dates.items()}
    events = defaultdict(set)
    for (etf, code), rec in by.items():
        ds = etf_dates[etf]
        last_k = -10 ** 9
        for k, d in enumerate(ds):
            r = rec.get(d)
            if not r or k - last_k < cooldown:
                continue
            win = [rec[x] for x in ds[max(0, k - W + 1): k + 1] if x in rec]
            buys = [x for x in win if x["act"] > 0 and x["act"] * x["px"] > 0]
            if len(buys) < min_days:
                continue
            amt = sum(x["act"] * x["px"] for x in buys)
            built = sum(x["act"] for x in buys) / r["shares"] if r["shares"] else 0
            if amt >= min_amt and built >= min_built and r["act"] > 0:   # 當天仍在買
                events[(d, code)].add(etf)
                last_k = k
    return events


def eval_events(dates, price, bench, di, events, L, date_filter=None):
    """回傳 {bucket或'全部': {h: [當日組合超額]}}，以及每筆的 runup。"""
    res = defaultdict(lambda: {h: defaultdict(list) for h in HORIZONS})
    for (d, code) in events:
        if date_filter and not date_filter(d):
            continue
        ru = runup_of(dates, price, bench, di, d, code, L)
        fr, _ = fwd_returns(dates, price, d, code)
        if fr is None:
            continue
        keys = ["全部"] + ([bucket_of(ru)] if ru is not None else [])
        for h in HORIZONS:
            b = bench(di[d] + 1, h)
            if fr[h] is None or b is None:
                continue
            for k in keys:
                res[k][h][d].append(fr[h] - b)
    return res


def fmt_cell(cohorts, h):
    s = summarize([st.mean(v) for v in cohorts.values()])
    if not s:
        return f"{'n/a':>8s} {'':4s} {'':4s}"
    return f"{pct(s['mean']):>8s} {s['hit']:4.0%} {s['t'] / math.sqrt(h):+4.1f}"


def print_table(title, res, n_events):
    print(f"\n  ▶ {title}（事件 {n_events}）")
    print(f"    {'前 L 日超額漲幅':16s} {'交易日':>5s}" + "".join(f" | {h:>2d}日超額 勝率  t值" for h in HORIZONS))
    for k in ["全部"] + [b[0] for b in RUNUP_BUCKETS]:
        if k not in res:
            continue
        n = len(res[k][HORIZONS[0]])
        print(f"    {k:16s} {n:5d}" + "".join(" | " + fmt_cell(res[k][h], h) for h in HORIZONS))


def report_accumulation(dates, price, rows, args):
    L = args.runup_days
    print("\n" + "=" * 78)
    print(f"[5] 多日累積＋排除追高（前 L={L} 個交易日的超額漲幅分組）")
    print("=" * 78)
    bench = bench_returns(dates, price)
    di = {d: i for i, d in enumerate(dates)}

    # 對照組：所有台股持股在同樣的前段漲幅下，之後本來就會怎樣（判斷「排除追高」是訊號的功勞還是動能/反轉本身）
    universe = {}
    for d in dates:
        for c in price[d]:
            universe[(d, c)] = None
    print_table("對照組：所有主動 ETF 台股持股（不管有沒有加碼）", eval_events(dates, price, bench, di, universe, L),
                len(universe))

    single = defaultdict(set)
    for r in rows:
        if (TW_CODE.match(r["code"]) and r["kind"] == "held"
                and r["active_pct"] >= args.min_pct / 100 and r["active_amt"] >= args.min_amt):
            single[(r["date"], r["code"])].add(r["etf"])
    print_table("單日主動加碼（上一段的 C）", eval_events(dates, price, bench, di, single, L), len(single))

    variants = [
        ("累積 A：5 天內 ≥3 天買、≥3000 萬、占部位 ≥10%", dict(W=5, min_days=3, min_amt=30e6, min_built=0.10)),
        ("累積 B：10 天內 ≥5 天買、≥5000 萬、占部位 ≥20%", dict(W=10, min_days=5, min_amt=50e6, min_built=0.20)),
        ("累積 C：5 天內 5 天都買、≥3000 萬", dict(W=5, min_days=5, min_amt=30e6, min_built=0.0)),
    ]
    acc_all = {}
    for title, kw in variants:
        ev = accumulation_events(rows, cooldown=args.cooldown, **kw)
        acc_all[title] = ev
        print_table(title, eval_events(dates, price, bench, di, ev, L), len(ev))
        multi = {k: v for k, v in ev.items() if len(v) >= 2}
        if len(multi) >= 20:
            print_table(title + "，且同日 ≥2 家", eval_events(dates, price, bench, di, multi, L), len(multi))

    # 前後半段：看結果是不是只在某一段成立
    mid = dates[len(dates) // 2]
    print(f"\n  ▶ 前後半段穩定度（以 {mid} 切開；只看「全部」與「前段沒漲 ≤0」兩組）")
    print(f"    {'訊號':44s} {'段':6s} {'組':8s} {'交易日':>5s} | 10日超額 勝率  t值 | 20日超額 勝率  t值")
    tests = [("對照組", universe), ("單日主動加碼", single)] + list(acc_all.items())
    for title, ev in tests:
        for half, f in (("前半", lambda d: d < mid), ("後半", lambda d: d >= mid)):
            res = eval_events(dates, price, bench, di, ev, L, f)
            not_chase = {h: defaultdict(list) for h in HORIZONS}
            for b in ("前段跌 ≤−5%", "小跌 −5%~0"):
                for h in HORIZONS:
                    for d, v in res.get(b, {}).get(h, {}).items():
                        not_chase[h][d].extend(v)
            for grp, rr in (("全部", res.get("全部")), ("沒漲≤0", not_chase)):
                if not rr:
                    continue
                print(f"    {title[:22]:44s} {half:6s} {grp:8s} {len(rr[10]):5d}"
                      + "".join(" | " + fmt_cell(rr[h], h) for h in (10, 20)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pct", type=float, default=1.0, help="主動比例門檻（%%）")
    ap.add_argument("--min-amt", type=float, default=10_000_000, help="主動金額門檻（元）")
    ap.add_argument("--baseline", choices=["units", "median"], default="median",
                    help="被動預期的基準：units＝單位數成長 g；median＝當天持股股數變化中位數（預設）")
    ap.add_argument("--csv", help="另存逐筆事件 CSV")
    ap.add_argument("--runup-days", type=int, default=10, help="追高判斷：看事件前幾個交易日的超額漲幅")
    ap.add_argument("--cooldown", type=int, default=10, help="多日累積：同一 ETF×股票觸發後幾份快照內不重複記")
    ap.add_argument("--accum-only", action="store_true", help="只跑 [5] 多日累積＋排除追高")
    args = ap.parse_args()

    dates, snaps, group = load()
    price = build_prices(dates, snaps)
    rows, days = decompose(dates, snaps, args.baseline)
    print(f"被動基準：{args.baseline}")
    print(f"快照 {len(dates)} 天（{dates[0]} ~ {dates[-1]}），持股異動列 {len(rows)} 筆\n")
    if not args.accum_only:
        report_alignment(days, group)
        report_cash(days)
        report_backtest(dates, price, rows, args)
        report_by_etf(dates, price, rows, args)
    report_accumulation(dates, price, rows, args)


if __name__ == "__main__":
    main()
