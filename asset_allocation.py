# -*- coding: utf-8 -*-
"""
解析統一投信系 ezmoney xlsx 開頭的「資產配置彙總」。

三檔（00981A / 00403A / 00988A）版面一致，範例：
    淨資產           NTD 287,544,965,248
    流通在外單位數    9,801,709,000
    每單位淨值        NTD 29.34
    項目 | 金額 | 權重
    期貨(名目本金)   NTD 6,097,827,400   2.12%
    股票            NTD 278,815,009,000  96.96%
    項目 | 金額
    現金            NTD 7,047,956,124
    期貨保證金       NTD 1,729,697,138   （00988A 可能為 USD）
    附買回債券       NTD 3,266,182,664
    應收付證券款     NTD 4,786,505,994   （可為負）
    期貨(名目本金)                       （僅在有期貨時出現）
    期貨代號 | 期貨名稱 | 持股權重
    TX | 台指期貨(B) | 2.12%
    股票            ← 持股表開始

以「標籤」掃描（不寫死列號），對版面微調較有韌性。權重一律以「佔淨資產(NTD)」計算。
"""
import re
import pandas as pd


def _num(cell):
    """從 'NTD 7,047,956,124' / 'USD 1,001,263.41' / '-348,558,982' 取 (金額float, 幣別)。"""
    if cell is None:
        return None, "NTD"
    s = str(cell).strip()
    ccy = "NTD"
    m = re.match(r"(NTD|USD|TWD)\s*", s)
    if m:
        ccy = m.group(1)
        s = s[m.end():]
    s = s.replace(",", "").strip()
    try:
        return float(s), ccy
    except ValueError:
        return None, ccy


def _pct(cell):
    """從 '2.12%' 取 2.12；失敗回傳 None。"""
    if not cell:
        return None
    try:
        return float(str(cell).replace("%", "").strip())
    except ValueError:
        return None


def parse_asset_allocation(xlsx_path, sheet=0):
    """
    回傳資產配置 dict（供塞進 data_*.json 的 meta.assetAllocation）；解析失敗回傳 {}。
    欄位皆為「佔淨資產」百分比（2 位小數）＋部分原始金額（元）。
    """
    try:
        df = pd.read_excel(xlsx_path, sheet_name=sheet, header=None)
    except Exception:
        return {}

    net = stock = fut_notional = cash = fut_margin = repo = recv = None
    stock_w = fut_notional_w = None
    fut_margin_ccy = "NTD"
    futures = []
    in_fut_detail = False

    for i in range(min(30, len(df))):
        c0 = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
        c1 = df.iloc[i, 1] if df.shape[1] > 1 and pd.notna(df.iloc[i, 1]) else None
        c2 = str(df.iloc[i, 2]).strip() if df.shape[1] > 2 and pd.notna(df.iloc[i, 2]) else ""
        val, ccy = _num(c1)

        if c0 == "股票代號":            # 進入持股明細 → 彙總結束
            break

        if "淨資產" in c0 and val is not None:
            net = val
        elif c0 == "股票" and val is not None and stock is None:
            stock, stock_w = val, _pct(c2)
        elif "期貨" in c0 and "名目本金" in c0 and "保證金" not in c0 and val is not None:
            fut_notional, fut_notional_w = val, _pct(c2)
        elif c0 == "現金" and val is not None:
            cash = val
        elif "期貨保證金" in c0 and val is not None:
            fut_margin, fut_margin_ccy = val, ccy
        elif "附買回" in c0 and val is not None:
            repo = val
        elif "應收付" in c0 and val is not None:
            recv = val
        elif c0 == "期貨代號":          # 期貨明細表頭
            in_fut_detail = True
        elif in_fut_detail:
            if c0 == "股票" or not c0:
                in_fut_detail = False
            elif c1 is not None:
                futures.append({"code": c0, "name": str(c1).strip(), "weight": _pct(c2)})

    if not net:
        return {}

    def pct(x):
        return round(x / net * 100, 2) if (x is not None and net) else None

    alloc = {
        "netAsset": int(net),
        "stockPct": stock_w if stock_w is not None else pct(stock),
        "cashPct": pct(cash),
        "cashAmount": int(cash) if cash is not None else None,
        "futuresNotionalPct": fut_notional_w if fut_notional_w is not None else pct(fut_notional),
        "repoBondPct": pct(repo),
        "netReceivablePct": pct(recv),
        # 期貨保證金：NTD 才換算佔比；USD 保留原值與幣別
        "futuresMarginPct": pct(fut_margin) if fut_margin_ccy == "NTD" else None,
        "futuresMargin": {"amount": int(fut_margin), "ccy": fut_margin_ccy} if fut_margin is not None else None,
        "futures": futures,          # [{code,name,weight}]，無期貨則為空陣列
        "hasFutures": bool(futures) or bool(fut_notional),
    }
    return alloc


def _xlsx_date(path, code):
    import os
    b = os.path.basename(path)
    pre = f"{code}_holdings_"
    return b[len(pre):-5] if (b.startswith(pre) and b.endswith(".xlsx")) else ""


def find_prev_alloc(holdings_dir, code, data_date):
    """找「data_date 之前」最近一個 xlsx，解析其資產配置。找不到回傳 {}。"""
    import glob
    import os
    dated = [(_xlsx_date(f, code), f)
             for f in glob.glob(os.path.join(holdings_dir, f"{code}_holdings_*.xlsx"))
             if "_temp" not in f]
    prev = sorted([(d, f) for d, f in dated if d and d < data_date])
    return parse_asset_allocation(prev[-1][1]) if prev else {}


def attach_delta(alloc, prev_alloc):
    """在 alloc 內加入 'delta'（今日 − 前一日，百分點 pp）；就地修改並回傳 alloc。"""
    if not alloc or not prev_alloc:
        return alloc
    d = {}
    for k in ("stockPct", "cashPct", "futuresNotionalPct",
              "repoBondPct", "netReceivablePct", "futuresMarginPct"):
        a, b = alloc.get(k), prev_alloc.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d[k] = round(a - b, 2)
    prevf = {f.get("code"): f.get("weight") for f in (prev_alloc.get("futures") or [])}
    fd = {}
    for f in (alloc.get("futures") or []):
        pw = prevf.get(f.get("code"))
        if isinstance(f.get("weight"), (int, float)) and isinstance(pw, (int, float)):
            fd[f["code"]] = round(f["weight"] - pw, 2)
    if fd:
        d["futures"] = fd
    if d:
        alloc["delta"] = d
    return alloc


def _fmt_pp(delta):
    """百分點變化 → '（↑0.31）'；|Δ|<0.01 視為持平回傳空字串。"""
    if delta is None or abs(delta) < 0.01:
        return ""
    return f"（{'↑' if delta > 0 else '↓'}{abs(delta):.2f}）"


def _fmt_zhang(n):
    """張數 → '7.0萬張' / '500張'（取絕對值格式化，符號另外處理）。"""
    n = abs(n)
    return f"{n / 10000:.1f}萬張" if n >= 10000 else f"{int(round(n)):,}張"


def format_scale_line(meta):
    """基金規模（在外流通張數）與較前一日的淨申購/淨贖回；回傳 list[str]。"""
    ts = meta.get("totalShares")
    pts = meta.get("prevTotalShares")
    if not ts:
        return []
    line = f"📈 基金規模：{_fmt_zhang(ts)}"
    if pts and pts > 0:
        diff = ts - pts
        pct = diff / pts * 100
        if abs(diff) < 500:                       # <0.5 張以下視為持平
            line += "（較前一日持平）"
        else:
            word = "淨申購" if diff > 0 else "淨贖回"
            arrow = "↑" if diff > 0 else "↓"
            line += f"（較前一日{word} {_fmt_zhang(diff)}，{arrow}{abs(pct):.1f}%）"
    mc = meta.get("totalMarketCap")
    pmc = meta.get("prevTotalMarketCap")
    if mc:
        seg = f"　💵 市值 {mc:,.1f}億"
        if pmc and pmc > 0 and abs(mc - pmc) >= 0.01:
            md = mc - pmc
            seg += f"（{'↑' if md > 0 else '↓'}{abs(md):.1f}億）"
        line += seg
    return [line]


def format_alloc_lines(alloc):
    """把資產配置 dict 轉成 Telegram 通知用的文字列（list[str]）；含與前一日的 pp 變化。無資料回傳 []。"""
    if not alloc or alloc.get("stockPct") is None:
        return []
    delta = alloc.get("delta") or {}
    parts = []
    for label, key in (("股票", "stockPct"), ("現金", "cashPct"),
                       ("附買回", "repoBondPct"), ("期貨保證金", "futuresMarginPct"),
                       ("應收付", "netReceivablePct")):
        v = alloc.get(key)
        if v is not None and v > 0.005:
            parts.append(f"{label} {v}%{_fmt_pp(delta.get(key))}")
    lines = []
    if parts:
        lines.append("🧩 資產配置（括號為較前一日 pp 變化）：" + "｜".join(parts))
    futs = alloc.get("futures") or []
    fdelta = delta.get("futures") or {}
    if futs:
        fl = "、".join(f"{f['code']} {f['name']} {f['weight']}%{_fmt_pp(fdelta.get(f['code']))}" for f in futs)
        lines.append(f"⚡ 期貨曝險：{fl}（名目本金，做多台股）")
    elif alloc.get("futuresNotionalPct"):
        lines.append(f"⚡ 期貨曝險 {alloc['futuresNotionalPct']}%{_fmt_pp(delta.get('futuresNotionalPct'))}（名目本金）")
    return lines


if __name__ == "__main__":
    import sys, json
    print(json.dumps(parse_asset_allocation(sys.argv[1]), ensure_ascii=False, indent=2))
