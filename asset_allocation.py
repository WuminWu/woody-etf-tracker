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


def format_alloc_lines(alloc):
    """把資產配置 dict 轉成 Telegram 通知用的文字列（list[str]）；無資料回傳 []。"""
    if not alloc or alloc.get("stockPct") is None:
        return []
    parts = []
    for label, key in (("股票", "stockPct"), ("現金", "cashPct"),
                       ("附買回", "repoBondPct"), ("期貨保證金", "futuresMarginPct"),
                       ("應收付", "netReceivablePct")):
        v = alloc.get(key)
        if v is not None and v > 0.005:
            parts.append(f"{label} {v}%")
    lines = []
    if parts:
        lines.append("🧩 資產配置：" + "｜".join(parts))
    futs = alloc.get("futures") or []
    if futs:
        fl = "、".join(f"{f['code']} {f['name']} {f['weight']}%" for f in futs)
        lines.append(f"⚡ 期貨曝險：{fl}（名目本金，做多台股）")
    elif alloc.get("futuresNotionalPct"):
        lines.append(f"⚡ 期貨曝險 {alloc['futuresNotionalPct']}%（名目本金）")
    return lines


if __name__ == "__main__":
    import sys, json
    print(json.dumps(parse_asset_allocation(sys.argv[1]), ensure_ascii=False, indent=2))
