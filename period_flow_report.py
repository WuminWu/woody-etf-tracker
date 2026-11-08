# -*- coding: utf-8 -*-
"""
period_flow_report.py
---------------------
統計主動式 ETF 在一段期間內「買了什麼、賣了什麼」，產生 Markdown 報告草稿（表格齊全，重點結論留給人寫）。
只讀 holdings/、snapshots/、etfs.json，不寫試算表、不發通知；出清當天沒有快照價格時會讀
證交所／櫃買收盤價（tw_prices，會寫 price_cache/）。

計算方式
  股數：逐日比較 holdings/{ETF}_holdings_{date}.json，當日 − 前一份持股檔。
        不用 snapshots 的 diffShares：快照只記 shares>0 的持股，出清那天的變化不在快照裡。
        跑完會拿逐日股數變化和快照 diffShares 對帳並印出結果。
  金額：每日 股數變化 × 當日收盤價 再加總（估算值，非實際成交金額）。
        價格順序：該 ETF 當日快照價 → 其他 ETF 當日快照價 → 證交所/櫃買收盤價 → 該股前一次出現的快照價。
        海外股在快照裡已換算新台幣。
  期初：START 之前最後一份持股檔。期間內才掛牌的 ETF（etfs.json 的 ipo 日 ≥ START）首份持股視為從零建倉；
        掛牌較早但期間內才開始追蹤的，只統計首份持股之後的變化。期貨部位（is_futures）不計入，另列口數。
  估算申購淨額：Σ（當日受益權單位數 − 前一日單位數）× 當日淨值（規模 ÷ 單位數），用每日快照 meta。

公司名稱連結
  預設依 Obsidian 筆記庫（VAULT_TOOLS）的正式名稱寫成 [[名稱(代號)]]；筆記庫沒有的用 NAME_FIX，
  再沒有就用 ETF 給的名稱（台股中文簡稱、海外英文名去掉 Inc/Corp 等字尾）。
  找不到筆記庫工具時自動退回後兩者。新出現、名稱不乾淨的公司會列在 summary.json 的 unlinked，可補進 NAME_FIX。

用法
  python period_flow_report.py --start 2026-10-08 --end 2026-11-06
      → period_flow_out/2026-10-08_2026-11-06/ 底下：report.md（報告草稿）、parts/*.md（各表格）、
        summary.json（重點數字，寫結論用）、flow.json（逐日明細）
  python period_flow_report.py --start ... --end ... --template 我的模板.md --output 報告.md
      → 模板裡的 {{ETF_TABLE}} {{TOP_BUY}} {{TOP_SELL}} {{CONS_BUY}} {{CONS_SELL}} {{NEW_MULTI}}
        {{EXIT_MULTI}} {{DETAIL}} {{FULL}} 換成表格
  --no-vault：不讀筆記庫，名稱只用 NAME_FIX 與 ETF 給的名稱

2026-11-08 首次用於知識筆記「2026-11-06 主動式ETF在股災期間買了什麼？」（2026-10-08～11-06）。
"""

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VAULT_TOOLS = Path(r"D:\vault_obsidian\vault-investment\tools")
sys.path.insert(0, str(ROOT))

TW_CODE = re.compile(r"\d{4,6}[A-Z]?")
PARTS = ["ETF_TABLE", "TOP_BUY", "TOP_SELL", "CONS_BUY", "CONS_SELL", "NEW_MULTI", "EXIT_MULTI", "DETAIL", "FULL"]

# 筆記庫還沒有正式名稱的公司：台股用證交所簡稱，海外用英文名（ETF 給的名稱太長或是中譯）。key 是筆記庫代號寫法。
NAME_FIX = {
    "1101": "台泥", "1216": "統一", "1402": "遠東新", "1605": "華新", "2412": "中華電", "2609": "陽明", "2615": "萬海",
    "2618": "長榮航", "2801": "彰銀", "2851": "中再保", "2881": "富邦金", "2882": "國泰金", "2883": "凱基金", "2884": "玉山金",
    "2885": "元大金", "2886": "兆豐金", "2887": "台新新光金", "2890": "永豐金", "2891": "中信金", "2892": "第一金",
    "3023": "信邦", "3045": "台灣大", "3090": "日電貿", "3617": "碩天", "3702": "大聯大", "4904": "遠傳", "5871": "中租-KY",
    "5876": "上海商銀", "5904": "寶雅", "6214": "精誠", "6446": "藥華藥", "6782": "視陽", "7751": "竑騰", "8070": "長華",
    "300757.SZ": "羅博特科", "3436.T": "SUMCO", "3697.T": "SHIFT", "6098.T": "Recruit", "6920.T": "Lasertec", "9602.T": "Toho",
    "AEM": "Agnico Eagle Mines", "BMNR": "BitMine Immersion", "COIN": "Coinbase", "CRCL": "Circle", "CRL": "Charles River Laboratories",
    "DASH": "DoorDash", "DBK.DE": "Deutsche Bank", "DDOG": "Datadog", "DT": "Dynatrace", "ESLT": "Elbit Systems", "FTNT": "Fortinet",
    "GH": "Guardant Health", "HOOD": "Robinhood", "HWM": "Howmet Aerospace", "IONQ": "IonQ", "JPM": "JPMorgan Chase",
    "LLY": "Eli Lilly", "MRK": "Merck", "MRNA": "Moderna", "NEE": "NextEra Energy", "NEM": "Newmont", "NOW": "ServiceNow",
    "NTRA": "Natera", "NTSK": "Netskope", "P": "Everpure", "PLTR": "Palantir", "PSX": "Phillips 66", "PWR": "Quanta Services",
    "RBRK": "Rubrik", "RKLB": "Rocket Lab", "RKT": "Rocket Companies", "RPRX": "Royalty Pharma", "SAP": "SAP",
    "SEI": "Solaris Energy Infrastructure", "SHOP": "Shopify", "SKHY": "SK Hynix", "SNOW": "Snowflake", "TEAM": "Atlassian",
    "THC": "Tenet Healthcare", "TWLO": "Twilio", "VEEV": "Veeva Systems", "WPM": "Wheaton Precious Metals", "ZETA": "Zeta Global",
    "ZS": "Zscaler",
}   # P = Everpure（Pure Storage 2026 改名，2026-04 起代號 P；nasdaq.com 新聞稿查證）


# ══════════════════════════════════════════════════════════════════════
# 1. 計算
# ══════════════════════════════════════════════════════════════════════

def load_snapshots(end):
    snap = {}
    d = ROOT / "snapshots"
    for f in sorted(os.listdir(d)):
        if re.fullmatch(r"\d{4}-\d\d-\d\d\.json", f) and f[:10] <= end:
            snap[f[:10]] = json.load(open(d / f, encoding="utf-8"))
    return snap


def load_holdings(end):
    hold = collections.defaultdict(dict)  # etf -> date -> {code: row}
    for f in os.listdir(ROOT / "holdings"):
        m = re.match(r"(\w+)_holdings_(\d{4}-\d\d-\d\d)\.json$", f)
        if not m or m.group(2) > end:
            continue
        rows = json.load(open(ROOT / "holdings" / f, encoding="utf-8"))
        hold[m.group(1)][m.group(2)] = {str(r["code"]).strip(): r for r in rows}
    return hold


def compute(start, end):
    etfs = json.load(open(ROOT / "etfs.json", encoding="utf-8"))
    snap = load_snapshots(end)
    snap_dates = sorted(snap)
    hold = load_holdings(end)

    price_etf, price_any = {}, collections.defaultdict(dict)
    for d, s in snap.items():
        for etf, v in s.items():
            for h in v.get("holdings", []):
                if h.get("price"):
                    price_etf[(d, etf, h["code"])] = h["price"]
                    price_any[d].setdefault(h["code"], h["price"])

    import tw_prices

    def price_of(etf, code, d):
        p = price_etf.get((d, etf, code)) or price_any.get(d, {}).get(code)
        if p:
            return p, "snap"
        if TW_CODE.fullmatch(code):
            p = tw_prices.close(code, d)
            if p:
                return p, "twse"
        for dd in reversed([x for x in snap_dates if x < d]):
            p = price_etf.get((dd, etf, code)) or price_any.get(dd, {}).get(code)
            if p:
                return p, "prev"
        return 0.0, "none"

    names = collections.defaultdict(collections.Counter)
    result = {"start": start, "end": end, "etfs": {}}
    for info in etfs:
        etf = info["code"]
        dates = sorted(hold.get(etf, {}))
        in_win = [d for d in dates if start <= d <= end]
        if not in_win:
            continue
        before = [d for d in dates if d < start]
        ipo = info["ipo"][0] if info.get("ipo") else None
        flags = []
        if before:
            base_date, from_zero = before[-1], False
        elif ipo and ipo >= start:
            base_date, from_zero = None, True
            flags.append(f"期間內新上市（{ipo} 掛牌），首份持股 {in_win[0]} 視為從零建倉")
        else:
            base_date, from_zero = in_win[0], False
            flags.append(f"追蹤資料自 {in_win[0]} 起，只統計 {in_win[0]} 之後的變化")
        if in_win[-1] < end:
            flags.append(f"最新資料日 {in_win[-1]}" + ("（T+1 揭露）" if info.get("t1") else ""))

        first = {} if from_zero else hold[etf][base_date]
        iter_dates = in_win if from_zero else [d for d in in_win if d > base_date]
        daily, prev = [], first
        for d in iter_dates:
            cur = hold[etf][d]
            for code in set(prev) | set(cur):
                r = cur.get(code) or prev.get(code)
                if r.get("is_futures"):
                    continue
                names[code][str(r.get("name", "")).strip()] += 1
                a = (prev.get(code) or {}).get("shares", 0) or 0
                b = (cur.get(code) or {}).get("shares", 0) or 0
                if a != b:
                    p, src = price_of(etf, code, d)
                    daily.append((d, code, b - a, p, (b - a) * p, src))
            prev = cur
        last = hold[etf][in_win[-1]]

        futures = {}
        for code in set(first) | set(last):
            r = last.get(code) or first.get(code)
            if r.get("is_futures"):
                futures[code] = {"name": r.get("name"), "start": (first.get(code) or {}).get("shares", 0),
                                 "end": (last.get(code) or {}).get("shares", 0)}

        per = {}
        for d, code, diff, p, amt, src in daily:
            x = per.setdefault(code, {"net_shares": 0, "buy_amt": 0.0, "sell_amt": 0.0, "buy_days": 0, "sell_days": 0})
            x["net_shares"] += diff
            if diff > 0:
                x["buy_amt"] += amt; x["buy_days"] += 1
            else:
                x["sell_amt"] += amt; x["sell_days"] += 1
        for code, x in per.items():
            x["start_shares"] = (first.get(code) or {}).get("shares", 0)
            x["end_shares"] = (last.get(code) or {}).get("shares", 0)
            assert x["start_shares"] + x["net_shares"] == x["end_shares"], (etf, code)

        meta = lambda d: snap[d][etf]["meta"] if d and d in snap and etf in snap[d] else None
        result["etfs"][etf] = {
            "name": info["name"], "group": info["group"], "t1": info.get("t1"), "ipo": ipo,
            "base_date": base_date, "first_date": in_win[0], "last_date": in_win[-1], "n_days": len(iter_dates),
            "from_zero": from_zero, "flags": flags, "meta_start": meta(base_date), "meta_end": meta(in_win[-1]),
            "stocks": per, "futures": futures, "daily": daily,
        }

    # 期間股價（快照中第一個與最後一個價格）
    prior = [x for x in snap_dates if x < start]
    p_dates = [x for x in snap_dates if (prior[-1] if prior else start) <= x <= end]
    allcodes = {c for v in result["etfs"].values() for c in v["stocks"]}
    prices = {}
    for c in allcodes:
        have = [(d, price_any[d][c]) for d in p_dates if c in price_any.get(d, {})]
        prices[c] = {"p0": have[0] if have else (None, None), "p1": have[-1] if have else (None, None)}
    result["prices"] = prices
    result["names"] = {c: names[c].most_common() for c in allcodes}

    # 對帳：逐日股數變化 vs 快照 diffShares（新上市首日建倉除外）
    checked = mism = 0
    src_count = collections.Counter()
    for etf, v in result["etfs"].items():
        for d, code, diff, p, amt, src in v["daily"]:
            src_count[src] += 1
            if v["from_zero"] and d == v["first_date"]:
                continue
            row = next((h for h in snap.get(d, {}).get(etf, {}).get("holdings", []) if h["code"] == code), None)
            if row is None:
                continue
            checked += 1
            if row.get("diffShares") != diff:
                mism += 1
                print(f"  對帳不符 {etf} {d} {code} 持股檔 {diff:+,} / 快照 {row.get('diffShares')}")
    result["check"] = {"checked": checked, "mismatch": mism, "price_src": dict(src_count)}
    print(f"對帳：{checked:,} 筆，不符 {mism}；價格來源 {dict(src_count)}")
    return result


# ══════════════════════════════════════════════════════════════════════
# 2. 產生表格
# ══════════════════════════════════════════════════════════════════════

def vault_ticker(code):
    if TW_CODE.fullmatch(code):
        return code
    sym, ex = code.rsplit(" ", 1) if " " in code else (code, "")
    return {"US": sym, "JP": sym + ".T", "KS": sym + ".KS", "KP": sym + ".KS",
            "CH": sym + (".SS" if sym.startswith("6") else ".SZ"), "GY": sym + ".DE",
            "HK": f"{int(sym):04d}.HK" if sym.isdigit() else code}.get(ex, code)


def load_canon(use_vault):
    if not use_vault or not VAULT_TOOLS.exists():
        return {}
    try:
        sys.path.insert(0, str(VAULT_TOOLS))
        import unify_links as U
        texts = {rel: f.read_text(encoding="utf-8", newline="") for rel, f in U.iter_md()}
        alias_values, overrides, stems = U.load_config()
        canon, _ = U.build_mapping(U.collect_targets(texts), stems, alias_values, overrides)
        return canon
    except Exception as e:
        print(f"⚠️ 讀不到筆記庫正式名稱（{e}），改用 NAME_FIX 與 ETF 名稱")
        return {}


SUFFIX = re.compile(r"(\s*[-.(].*$)|(\s+(inc|corp|corporation|co|ltd|plc|sa|nv|ag|se|holdings?|group|company|limited|"
                    r"technologies|technology|holding)\b.*$)", re.I)


def render(flow, canon):
    E = flow["etfs"]

    def display(code):
        vt = vault_ticker(code)
        if vt in canon:
            return f"[[{canon[vt]}]]"
        if vt in NAME_FIX:
            return f"[[{NAME_FIX[vt]}({vt})]]"
        nm = [n for n, _ in flow["names"].get(code, []) if n]
        if TW_CODE.fullmatch(code):
            name = nm[0].rstrip("*").strip() if nm else code
        else:
            en = [n for n in nm if not re.search(r"[\u4e00-\u9fff]", n)]
            name = SUFFIX.sub("", en[0]).strip(" ,-") if en else (nm[0] if nm else code)
            name = name.title() if name.isupper() else name
        return f"[[{name}({vt})]]"

    market = lambda c: "台股" if TW_CODE.fullmatch(c) else "海外"
    fmt_i = lambda x: f"{int(round(x)):,}"
    wan = lambda x: f"{x / 1e4:,.0f}"
    yi = lambda x: f"{x / 1e8:,.2f}"

    def action(s0, s1):
        if s0 == 0 and s1 > 0: return "新建倉"
        if s0 > 0 and s1 == 0: return "出清"
        if s1 > s0: return "加碼"
        if s1 < s0: return "減碼"
        return "買賣互抵"

    def pchg(code):
        p = flow["prices"].get(code) or {}
        (d0, p0), (d1, p1) = p.get("p0") or (None, None), p.get("p1") or (None, None)
        return f"{(p1 / p0 - 1) * 100:+.1f}%" if p0 and p1 and d0 != d1 else "—"

    # 每檔 ETF 摘要與全體合計
    summ = {}
    agg = collections.defaultdict(lambda: {"net_shares": 0, "net_amt": 0.0, "buy_amt": 0.0, "sell_amt": 0.0,
                                           "buyers": [], "sellers": [], "new": [], "exit": [], "by_etf": {}})
    for etf, v in E.items():
        st = v["stocks"]
        m0, m1 = v["meta_start"], v["meta_end"]
        summ[etf] = dict(
            buy=sum(x["buy_amt"] for x in st.values()), sell=sum(x["sell_amt"] for x in st.values()),
            n_new=sum(1 for x in st.values() if x["start_shares"] == 0 < x["end_shares"]),
            n_exit=sum(1 for x in st.values() if x["start_shares"] > 0 == x["end_shares"]),
            n_add=sum(1 for x in st.values() if x["end_shares"] > x["start_shares"]),
            n_cut=sum(1 for x in st.values() if x["end_shares"] < x["start_shares"]),
            u0=m0 and m0.get("totalShares"), u1=m1 and m1.get("totalShares"),
            cap0=m0 and m0.get("totalMarketCap"), cap1=m1 and m1.get("totalMarketCap"))
        summ[etf]["net"] = summ[etf]["buy"] + summ[etf]["sell"]
        for code, x in st.items():
            a = agg[code]
            a["net_shares"] += x["net_shares"]; a["net_amt"] += x["buy_amt"] + x["sell_amt"]
            a["buy_amt"] += x["buy_amt"]; a["sell_amt"] += x["sell_amt"]
            a["by_etf"][etf] = (x["buy_amt"] + x["sell_amt"], action(x["start_shares"], x["end_shares"]))
            if x["net_shares"] > 0: a["buyers"].append(etf)
            elif x["net_shares"] < 0: a["sellers"].append(etf)
            if x["start_shares"] == 0 < x["end_shares"]: a["new"].append(etf)
            if x["start_shares"] > 0 == x["end_shares"]: a["exit"].append(etf)

    # 估算申購淨額
    snap = load_snapshots(flow["end"])
    inflow = {}
    for etf, v in E.items():
        if v["from_zero"]:
            inflow[etf] = None
            continue
        days = [d for d in sorted(snap) if (v["base_date"] or v["first_date"]) <= d <= v["last_date"] and etf in snap[d]]
        tot, prev = 0.0, None
        for d in days:
            m = snap[d][etf]["meta"]
            if prev and m.get("totalShares") and prev.get("totalShares") and m.get("totalMarketCap"):
                tot += (m["totalShares"] - prev["totalShares"]) * m["totalMarketCap"] * 1e8 / m["totalShares"]
            prev = m
        inflow[etf] = tot if len(days) > 1 else None

    parts = {}
    rows = ["| ETF | 統計區間 | 買進金額（億） | 賣出金額（億） | 淨買賣（億） | 估算申購淨額（億） | 加碼／減碼檔數 | 新建倉／出清 | 受益權單位變化 | 規模（億，期初→期末） | 備註 |",
            "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
    for etf, v in E.items():
        s = summ[etf]
        uchg = f"{(s['u1'] / s['u0'] - 1) * 100:+.1f}%" if s["u0"] and s["u1"] else "—"
        cap = f"{s['cap0']:,.0f}→{s['cap1']:,.0f}" if s["cap0"] and s["cap1"] else (f"→{s['cap1']:,.0f}" if s["cap1"] else "—")
        inf = yi(inflow[etf]) if inflow[etf] is not None else "—"
        rows.append(f"| {etf} {v['name']} | {v['first_date'][5:]}～{v['last_date'][5:]} | {yi(s['buy'])} | {yi(abs(s['sell']))} | "
                    f"{yi(s['net'])} | {inf} | {s['n_add']}／{s['n_cut']} | {s['n_new']}／{s['n_exit']} | {uchg} | {cap} | {'；'.join(v['flags'])} |")
    tot_buy = sum(s["buy"] for s in summ.values()); tot_sell = sum(s["sell"] for s in summ.values())
    tot_inflow = sum(x for x in inflow.values() if x is not None)
    rows.append(f"| **合計** |  | **{yi(tot_buy)}** | **{yi(abs(tot_sell))}** | **{yi(tot_buy + tot_sell)}** | **{yi(tot_inflow)}** |  |  |  |  |  |")
    parts["ETF_TABLE"] = "\n".join(rows)

    head = ("| # | 標的 | 市場 | 淨增減股數 | 淨買賣（億） | 買進（億） | 賣出（億） | 淨買／淨賣 ETF 數 | 期間股價 |\n"
            "|--:|---|---|--:|--:|--:|--:|--:|--:|")
    agg_row = lambda i, c, a: (f"| {i} | {display(c)} | {market(c)} | {fmt_i(a['net_shares'])} | {yi(a['net_amt'])} | "
                               f"{yi(a['buy_amt'])} | {yi(abs(a['sell_amt']))} | {len(a['buyers'])}／{len(a['sellers'])} | {pchg(c)} |")
    agg_table = lambda items: head + "\n" + "\n".join(agg_row(i + 1, c, a) for i, (c, a) in enumerate(items))
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["net_amt"], kv[0]))
    top_buy = [kv for kv in ranked if kv[1]["net_amt"] > 0][:30]
    top_sell = [kv for kv in reversed(ranked) if kv[1]["net_amt"] < 0][:30]
    parts["TOP_BUY"], parts["FULL"] = agg_table(top_buy), agg_table(ranked)

    # 淨賣超表多一欄：是哪些 ETF 賣的（淨賣金額由大到小，出清另外標註）
    def sellers_txt(a):
        out = []
        for e in sorted(a["sellers"], key=lambda e: a["by_etf"][e][0]):
            amt, act = a["by_etf"][e]
            out.append(f"{e}（{yi(amt)}{'，出清' if act == '出清' else ''}）")
        return "、".join(out)
    parts["TOP_SELL"] = ("| # | 標的 | 市場 | 淨增減股數 | 淨買賣（億） | 買進（億） | 賣出（億） | 淨買／淨賣 ETF 數 | 期間股價 | 淨賣的 ETF（淨賣金額，億） |\n"
                         "|--:|---|---|--:|--:|--:|--:|--:|--:|---|\n"
                         + "\n".join(agg_row(i + 1, c, a) + f" {sellers_txt(a)} |" for i, (c, a) in enumerate(top_sell)))

    def cons_table(key, other, label, olabel, sign):
        items = sorted([kv for kv in agg.items() if len(kv[1][key]) >= 5],
                       key=lambda kv: (-len(kv[1][key]), -sign * kv[1]["net_amt"], kv[0]))
        out = [f"| 標的 | {label} ETF 數 | {olabel} ETF 數 | 淨買賣（億） | {label}的 ETF |", "|---|--:|--:|--:|---|"]
        out += [f"| {display(c)} | {len(a[key])} | {len(a[other])} | {yi(a['net_amt'])} | {'、'.join(a[key])} |" for c, a in items]
        return "\n".join(out)
    parts["CONS_BUY"] = cons_table("buyers", "sellers", "淨買", "淨賣", 1)
    parts["CONS_SELL"] = cons_table("sellers", "buyers", "淨賣", "淨買", -1)

    def ne_table(key, label, sign):
        items = sorted([kv for kv in agg.items() if len(kv[1][key]) >= 2],
                       key=lambda kv: (-len(kv[1][key]), -sign * kv[1]["net_amt"], kv[0]))
        out = [f"| 標的 | {label}的 ETF 數 | 淨買賣（億） | ETF |", "|---|--:|--:|---|"]
        out += [f"| {display(c)} | {len(a[key])} | {yi(a['net_amt'])} | {'、'.join(a[key])} |" for c, a in items]
        return "\n".join(out)
    parts["NEW_MULTI"] = ne_table("new", "新建倉", 1)
    parts["EXIT_MULTI"] = ne_table("exit", "出清", -1)

    detail = []
    for etf, v in E.items():
        s = summ[etf]
        detail.append(f"### {etf} {v['name']}\n")
        detail.append(f"統計 {v['first_date']}～{v['last_date']}（{v['n_days']} 個交易日的變化）；"
                      f"買進 {yi(s['buy'])} 億、賣出 {yi(abs(s['sell']))} 億、淨買賣 {yi(s['net'])} 億；"
                      f"加碼 {s['n_add']} 檔、減碼 {s['n_cut']} 檔（含新建倉 {s['n_new']}、出清 {s['n_exit']}）。")
        if v["flags"]:
            detail.append("<br>⚠️ " + "；".join(v["flags"]) + "。")
        for fx in v["futures"].values():
            detail.append(f"<br>期貨：{fx['name']} 期初 {fx['start']} 口、期末 {fx['end']} 口（不列入下表）。")
        detail.append("")
        if not v["stocks"]:
            detail.append("期間內持股股數沒有變動。\n")
            continue
        detail.append("| 標的 | 動作 | 期初股數 | 期末股數 | 淨增減股數 | 淨買賣（萬元） | 買進日／賣出日 |")
        detail.append("|---|---|--:|--:|--:|--:|--:|")
        for code, x in sorted(v["stocks"].items(), key=lambda kv: (-(kv[1]["buy_amt"] + kv[1]["sell_amt"]), kv[0])):
            detail.append(f"| {display(code)} | {action(x['start_shares'], x['end_shares'])} | {fmt_i(x['start_shares'])} | "
                          f"{fmt_i(x['end_shares'])} | {fmt_i(x['net_shares'])} | {wan(x['buy_amt'] + x['sell_amt'])} | "
                          f"{x['buy_days']}／{x['sell_days']} |")
        detail.append("")
    parts["DETAIL"] = "\n".join(detail).rstrip()

    tw = [a for c, a in agg.items() if market(c) == "台股"]
    ov = [a for c, a in agg.items() if market(c) == "海外"]
    newfund = sorted(e for e, v in E.items() if v["from_zero"])
    summary = {
        "總買進（億）": tot_buy / 1e8, "總賣出（億）": tot_sell / 1e8, "淨買賣（億）": (tot_buy + tot_sell) / 1e8,
        "估算申購淨額（億，不含新上市）": tot_inflow / 1e8,
        "新上市 ETF": newfund, "新上市建倉淨額（億）": sum(summ[e]["net"] for e in newfund) / 1e8,
        "台股標的 買／賣（億）": [sum(a["buy_amt"] for a in tw) / 1e8, sum(a["sell_amt"] for a in tw) / 1e8],
        "海外標的 買／賣（億）": [sum(a["buy_amt"] for a in ov) / 1e8, sum(a["sell_amt"] for a in ov) / 1e8],
        "標的數（台股／海外）": [len(tw), len(ov)],
        "淨買前15": [(display(c), round(a["net_amt"] / 1e8, 2), len(a["buyers"]), len(a["sellers"]), pchg(c)) for c, a in top_buy[:15]],
        "淨賣前15": [(display(c), round(a["net_amt"] / 1e8, 2), len(a["buyers"]), len(a["sellers"]), pchg(c)) for c, a in top_sell[:15]],
        "各ETF（淨買賣、申購淨額，億）": {e: [round(summ[e]["net"] / 1e8, 2), inflow[e] and round(inflow[e] / 1e8, 2)] for e in E},
        "對帳": flow["check"],
        "unlinked": sorted({display(c) for c in agg if vault_ticker(c) not in canon and vault_ticker(c) not in NAME_FIX}),
    }
    return parts, summary


DEFAULT_TEMPLATE = """---
type: 知識筆記
date: {end}
source: 自己整理
tags: [主動式ETF, ETF籌碼, 資金流向]
---

# 主動式ETF在 {start}～{end} 買了什麼

> 統計期間 {start}～{end}，資料來自 ETF_Tracker 資料庫（每日持股 `holdings/`、每日快照 `snapshots/`），由 `period_flow_report.py` 產生。

## 一、重點結論

> ⚠️ 待補：依 summary.json 的數字撰寫。

## 二、統計方法與注意事項

1. **股數**：<br>逐日比較每檔 ETF 的持股檔，當日股數 − 前一交易日股數（含出清）。與快照 diffShares 對帳 {checked:,} 筆，不符 {mismatch} 筆。

2. **金額**：<br>每日股數變化 × 當日收盤價（海外股已換算新台幣）加總，是估算值；沒有快照價格時改用證交所／櫃買收盤價或前一次價格（共 {fallback} 筆）。

3. **期初基準**：<br>期間開始前最後一份持股；期間內新上市的 ETF 首份持股視為從零建倉，計入買進。

4. **估算申購淨額**：<br>Σ（當日受益權單位數 − 前一日單位數）× 當日淨值（規模 ÷ 單位數）；新上市 ETF 不估算。

## 三、各 ETF 買賣總覽

{{{{ETF_TABLE}}}}

## 四、全體 ETF 合計

### 4.1 淨買超前 30 名

{{{{TOP_BUY}}}}

### 4.2 淨賣超前 30 名

{{{{TOP_SELL}}}}

### 4.3 多檔 ETF 一起買（5 檔以上淨買）

{{{{CONS_BUY}}}}

### 4.4 多檔 ETF 一起賣（5 檔以上淨賣）

{{{{CONS_SELL}}}}

### 4.5 2 檔以上 ETF 新建倉

{{{{NEW_MULTI}}}}

### 4.6 2 檔以上 ETF 出清

{{{{EXIT_MULTI}}}}

## 五、各 ETF 買賣明細

> 依淨買賣金額由大到小排列；「買進日／賣出日」是期間內加碼、減碼的天數。

{{{{DETAIL}}}}

## 附錄：全體合計完整清單

{{{{FULL}}}}
"""


def main():
    ap = argparse.ArgumentParser(description="主動式 ETF 期間買賣統計")
    ap.add_argument("--start", required=True, help="期間第一天 YYYY-MM-DD（該日的買賣會計入）")
    ap.add_argument("--end", required=True, help="期間最後一天 YYYY-MM-DD")
    ap.add_argument("--out-dir", help="輸出資料夾（預設 period_flow_out/{start}_{end}）")
    ap.add_argument("--template", help="報告模板（含 {{ETF_TABLE}} 等佔位符）；不給就用預設草稿")
    ap.add_argument("--output", help="組好的報告存哪裡（預設 out-dir/report.md）")
    ap.add_argument("--no-vault", action="store_true", help="不讀筆記庫正式名稱")
    a = ap.parse_args()

    out = Path(a.out_dir) if a.out_dir else ROOT / "period_flow_out" / f"{a.start}_{a.end}"
    (out / "parts").mkdir(parents=True, exist_ok=True)

    flow = compute(a.start, a.end)
    json.dump(flow, open(out / "flow.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    parts, summary = render(flow, load_canon(not a.no_vault))
    for k, t in parts.items():
        (out / "parts" / f"{k}.md").write_text(t, encoding="utf-8")
    json.dump(summary, open(out / "summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if a.template:
        tpl = Path(a.template).read_text(encoding="utf-8")
    else:
        src = flow["check"]["price_src"]
        tpl = DEFAULT_TEMPLATE.format(start=a.start, end=a.end, checked=flow["check"]["checked"],
                                      mismatch=flow["check"]["mismatch"],
                                      fallback=sum(v for k, v in src.items() if k != "snap"))
    for k in PARTS:
        tpl = tpl.replace("{{%s}}" % k, parts[k].rstrip())
    report = Path(a.output) if a.output else out / "report.md"
    report.write_text(tpl, encoding="utf-8")

    print(f"淨買賣 {summary['淨買賣（億）']:,.2f} 億（買 {summary['總買進（億）']:,.2f}、賣 {summary['總賣出（億）']:,.2f}）")
    if summary["unlinked"]:
        print(f"筆記庫與 NAME_FIX 都沒有的名稱 {len(summary['unlinked'])} 個（見 summary.json 的 unlinked）")
    print(f"報告：{report}\n明細：{out}")


if __name__ == "__main__":
    main()
