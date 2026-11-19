# -*- coding: utf-8 -*-
"""
alloc_sources.py — 非統一投信系 ETF 的「資產配置」（股票／現金／期貨／保證金／應收付…）。

統一投信系（00403A / 00411A / 00981A / 00988A）由 asset_allocation.py 解析 ezmoney xlsx；
其餘各投信的來源不同，這裡把它們都轉成**同一份 schema**，好讓 format_alloc_lines（Telegram）
與 app.js（網站資產配置面板）不用分家：

    netAsset, stockPct, cashPct, repoBondPct, futuresMarginPct, netReceivablePct,
    futuresNotionalPct, futures=[{code,name,contracts,weight,month}], hasFutures,
    optionsPct, options=[...], cashEstimated(bool), source, dataDate, delta{...}

百分比一律「佔基金淨資產」。來源有給權重就用來源的，沒有才用 金額/淨資產 自己算。

## 各投信來源（2026-11 實測）
| 投信 | ETF | 來源 |
|---|---|---|
| 國泰 | 00400A | cwapi GetETFDetailBalList（現金/保證金/申贖應付款/股票 金額）＋ GetETFDetailFutureList |
| 安聯 | 00402A 00412A 00984A 00993A | GetFundAssets（腳本已用 Playwright 攔截）：股票% 在表頭、附買回債券、期貨表；**沒有現金欄 → 現金=推算** |
| 富邦 | 00405A | Assets.aspx 頁面：股票合計、保證金、現金、應收(付)證券款 |
| 中信 | 00406A 00995A | ETFHoldingWeight（腳本已抓）：STOCK/FUTURE/MARGIN/OPTION/CASH 各段都有權重 |
| 凱基 | 00407A | xlsx 只有股票，**沒有現金/期貨 → 股票=權重加總、現金及其他=推算** |
| 復華 | 00409A 00418A 00991A | /api/assets：股票、各幣別現金餘額（原幣，用 rate 表換算）、應付申購/買回款 |
| 玉山 | 00413A | GetFundAssets 彙總表（同安聯版型） |
| 群益 | 00415A 00982A 00992A 00997A | xlsx「其他資產」sheet：現金/保證金/應收付（可能是 USD → 用 yfinance 匯率換算） |
| 台新 | 00416A 00987A | ETFSeriesDetail 頁面：股票合計%、期貨表、現金流動比率% |
| 野村 | 00980A 00985A 00999A | GetFundAssets 彙總表＋期貨表（含口數） |
| 元大 | 00990A | 頁面 __NUXT__：FundWeights.Summary＋Cash.CashPosition（rto 已是 %） |
| 兆豐 | 00996A | etf_product.aspx 頁面：股票(%)、現金/存款、應收付證券款、保證金 |

## 與前一日比較
每次成功取得就存 holdings/{code}_alloc_{YYYY-MM-DD}.json；delta 與「資料日之前最近一份」比。
來源可指定日期者（國泰／中信／復華／野村），第一次跑時會順便補抓前一交易日，當天就有 delta。

所有對外入口（get_alloc）都吞掉例外回傳 {}：資產配置只是附加資訊，絕不能讓持股更新失敗。
"""

import glob
import io
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

HOLDINGS_DIR = "holdings"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


# ============================================================================
# 小工具
# ============================================================================

def _num(s):
    """'TWD$ 1,234.5' / '(733,266)' / 'NT$-1,041' / '-8,288.00' → float；失敗 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^\d.\-]", "", t)
    if t in ("", "-", ".", "-."):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -abs(v) if neg else v


def _pct(s):
    return _num(str(s).replace("%", "")) if s is not None else None


def _r2(x):
    return round(x, 2) if isinstance(x, (int, float)) else None


def _get(url, headers=None, data=None, timeout=30):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = dict(headers or {})
    if body is not None:
        hdr["Content-Type"] = "application/json"
    return json.loads(_get(url, hdr, data))


def _html_text_lines(html):
    """HTML → 非空白文字列（依標籤切開）。"""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    parts = re.split(r"<[^>]+>", html)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p.replace("&nbsp;", " ")).strip()
        if p:
            out.append(p)
    return out


_FX_CACHE = {}


def _fx_to_twd(ccy):
    """外幣 → 新台幣匯率（yfinance）；失敗 None。TWD/NTD 回 1。"""
    ccy = (ccy or "").upper().replace("NT$", "TWD")
    if ccy in ("", "TWD", "NTD"):
        return 1.0
    if ccy in _FX_CACHE:
        return _FX_CACHE[ccy]
    rate = None
    try:
        import yfinance as yf
        h = yf.Ticker(f"{ccy}TWD=X").history(period="5d")
        if len(h):
            rate = float(h["Close"].dropna().iloc[-1])
    except Exception as e:
        log.warning(f"匯率 {ccy}TWD 取得失敗：{e}")
    _FX_CACHE[ccy] = rate
    return rate


def _futures_list(rows):
    """[{code,name,contracts,weight,month}] 清掉 None 欄位。"""
    out = []
    for f in rows:
        f = {k: v for k, v in f.items() if v not in (None, "")}
        if f.get("code") or f.get("name"):
            out.append(f)
    return out


def make_alloc(net, source, data_date="", *, stock_pct=None, stock=None,
               cash_pct=None, cash=None, margin_pct=None, margin=None,
               repo_pct=None, repo=None, recv_pct=None, recv=None,
               fut_notional_pct=None, fut_notional=None, futures=None,
               options=None, options_pct=None, cash_estimated=False):
    """把各來源的金額／權重組成統一 schema。*_pct 優先；沒有就用金額/淨資產。"""
    def pct(p, amt):
        if p is not None:
            return _r2(p)
        if amt is not None and net:
            return _r2(amt / net * 100)
        return None

    futures = _futures_list(futures or [])
    fnp = pct(fut_notional_pct, fut_notional)
    if fnp is None and futures and all(isinstance(f.get("weight"), (int, float)) for f in futures):
        fnp = _r2(sum(f["weight"] for f in futures))
    alloc = {
        "source": source,
        "dataDate": data_date,
        "netAsset": int(net) if net else None,
        "stockPct": pct(stock_pct, stock),
        "cashPct": pct(cash_pct, cash),
        "cashAmount": int(cash) if cash is not None else None,
        "repoBondPct": pct(repo_pct, repo),
        "futuresMarginPct": pct(margin_pct, margin),
        "netReceivablePct": pct(recv_pct, recv),
        "futuresNotionalPct": fnp if fnp else None,
        "futures": futures,
        "hasFutures": bool(futures) or bool(fnp),
    }
    if options:
        alloc["options"] = _futures_list(options)
        alloc["optionsPct"] = _r2(options_pct) if options_pct is not None else _r2(
            sum(o.get("weight") or 0 for o in options))
    if cash_estimated:
        alloc["cashEstimated"] = True
    return {k: v for k, v in alloc.items() if v is not None}


def _estimate_cash(alloc):
    """來源沒給現金：現金及其他 ≈ 100 − 股票 − 附買回 − 保證金 − 應收付（期貨名目本金不佔資產）。"""
    if alloc.get("stockPct") is None or alloc.get("cashPct") is not None:
        return alloc
    used = sum(alloc.get(k) or 0 for k in ("stockPct", "repoBondPct", "futuresMarginPct", "netReceivablePct"))
    alloc["cashPct"] = _r2(100 - used)
    alloc["cashEstimated"] = True
    return alloc


# ============================================================================
# 各投信解析器：parse_*（吃原始資料，純函式，可測）／ fetch_*（自己抓）
# ============================================================================

# ---- 國泰 cwapi ----
CATHAY_API = "https://cwapi.cathaysite.com.tw/api/ETF"
CATHAY_HDR = {"Referer": "https://www.cathaysite.com.tw/"}


def parse_cathay(bal_rows, fut_rows, net, data_date=""):
    stock = cash = margin = recv = None
    stock_pct = None
    for r in bal_rows or []:
        item = str(r.get("item") or "").strip()
        amt = _num(r.get("amount"))
        if amt is None:
            continue
        if item == "股票":
            stock, stock_pct = amt, _pct(r.get("ntMkval"))
        elif "現金" in item:
            cash = (cash or 0) + amt
        elif "保證金" in item:
            margin = (margin or 0) + amt
        elif "應付" in item or "應收" in item:
            recv = (recv or 0) + amt
    futures = []
    for r in fut_rows or []:
        futures.append({
            "code": str(r.get("stockCode") or r.get("futureCode") or r.get("code") or "").strip(),
            "name": str(r.get("stockName") or r.get("futureName") or r.get("name") or "").strip(),
            "contracts": int(_num(r.get("volumn") or r.get("qty")) or 0) or None,
            "weight": _pct(r.get("weights") or r.get("weight")),
        })
    return make_alloc(net, "cathay", data_date, stock_pct=stock_pct, stock=stock, cash=cash,
                      margin=margin, recv=recv, futures=futures)


def fetch_cathay(fund_code, data_date):
    q = urllib.parse.urlencode({"FundCode": fund_code, "SearchDate": data_date.replace("-", "/")})
    bal = _get_json(f"{CATHAY_API}/GetETFDetailBalList?{q}", CATHAY_HDR).get("result") or []
    fut = _get_json(f"{CATHAY_API}/GetETFDetailFutureList?{q}", CATHAY_HDR).get("result") or []
    if not bal:
        return {}
    # BalList 只有金額；淨資產用 GetETFAssets（只給最新一天）或 股票金額/股票% 反推
    net = None
    stock_row = next((r for r in bal if str(r.get("item")).strip() == "股票"), None)
    if stock_row and _pct(stock_row.get("ntMkval")):
        net = _num(stock_row["amount"]) / (_pct(stock_row["ntMkval"]) / 100)
    if not net:
        res = _get_json(f"{CATHAY_API}/GetETFAssets?fundCode={fund_code}", CATHAY_HDR).get("result") or {}
        if (res.get("preDate") or "").replace("/", "-") == data_date:
            net = _num(res.get("fundNav"))
    if not net:
        return {}
    return parse_cathay(bal, fut, net, data_date)


# ---- MoneyDJ 版型 GetFundAssets（安聯／野村／玉山） ----

def parse_fundassets(raw):
    """Entries.Data = {FundAsset{Aum,NavDate}, Table[...]}。
    彙總表（TableTitle 空白）：股票/期貨(名目)/現金/保證金/應收(付)證券款 的金額；
    「股票 (97.66%)」表頭帶股票權重；「期貨」表含口數；「附買回債券」表只有金額。"""
    data = ((raw or {}).get("Entries") or {}).get("Data") or {}
    fa = data.get("FundAsset") or {}
    net = _num(fa.get("Aum"))
    data_date = str(fa.get("NavDate") or "").replace("/", "-")
    if not net:
        return {}
    kw = {}
    futures, repo = [], None
    for t in data.get("Table") or []:
        title = str(t.get("TableTitle") or "").strip()
        cols = [str(c.get("Name") or "").strip() for c in (t.get("Columns") or [])]
        rows = t.get("Rows") or []
        if not title:                                   # 彙總表
            for r in rows:
                if not r or not str(r[0]).strip():
                    continue
                label = str(r[0]).strip()
                amt = _num(r[3] if len(r) > 3 else r[1])
                if amt is None:
                    amt = _num(r[1]) if len(r) > 1 else None
                if amt is None:
                    continue
                if label == "股票":
                    kw["stock"] = amt
                elif label.startswith("期貨"):
                    kw["fut_notional"] = amt if amt else None
                elif "現金" in label:
                    kw["cash"] = (kw.get("cash") or 0) + amt
                elif "保證金" in label:
                    kw["margin"] = (kw.get("margin") or 0) + amt
                elif "應收" in label or "應付" in label:
                    kw["recv"] = (kw.get("recv") or 0) + amt
                elif "附買回" in label:
                    repo = (repo or 0) + amt
        elif title.startswith("股票"):
            m = re.search(r"([\d.]+)\s*%", title)
            if m:
                kw["stock_pct"] = float(m.group(1))
        elif title.startswith("期貨"):
            ci = {n: i for i, n in enumerate(cols)}
            def col(r, *names):
                for n in names:
                    for k, i in ci.items():
                        if n in k and i < len(r):
                            return r[i]
                return None
            for r in rows:
                futures.append({
                    "code": str(col(r, "代碼", "代號") or "").strip(),
                    "name": str(col(r, "名稱") or "").strip(),
                    "contracts": int(_num(col(r, "口數")) or 0) or None,
                    "weight": _pct(col(r, "權重")),
                    "month": str(col(r, "年月", "到期") or "").strip() or None,
                })
        elif "附買回" in title:
            ci = {n: i for i, n in enumerate(cols)}
            j = next((i for n, i in ci.items() if "金額" in n), len(cols) - 1)
            repo = (repo or 0) + sum(_num(r[j]) or 0 for r in rows if j < len(r))
    alloc = make_alloc(net, "fundassets", data_date, repo=repo, futures=futures, **kw)
    return _estimate_cash(alloc)


NOMURA_API = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
ESUN_API = "https://www.esunam.com/ETFAPI/GetFundAssets"


def fetch_nomura(code, data_date):
    return parse_fundassets(_get_json(NOMURA_API, body={"FundID": code, "SearchDate": data_date}))


def fetch_esun(fund_id, data_date=""):
    return parse_fundassets(_get_json(ESUN_API, body={"FundID": fund_id}))


# ---- 中信 ETFHoldingWeight ----

def parse_ctbc(raw):
    d = (raw or {}).get("Data", raw) or {}
    fa = (d.get("FundAssets") or [{}])[0]
    net = _num(fa.get("基金淨資產"))
    data_date = str(fa.get("NAV_DT") or fa.get("資料日期") or "")[:10].replace("/", "-")
    if not net:
        return {}

    def w(item):
        p = _pct(item.get("weights_"))
        amt = _num(item.get("amount_"))
        if (not p) and amt:                     # 應收(付)證券款 的 weights_ 常是 0.00，自己算
            return amt / net * 100
        return p

    kw, futures, options = {}, [], []
    for sec in d.get("FundAssetsDetail") or []:
        code = sec.get("Code")
        items = sec.get("Data") or []
        if code == "STOCK":
            kw["stock_pct"] = _num(sec.get("Sum")) or sum(_pct(i.get("weights_")) or 0 for i in items)
        elif code == "FUTURE":
            for i in items:
                futures.append({"code": str(i.get("code_") or "").strip(), "name": str(i.get("name_") or "").strip(),
                                "contracts": int(_num(i.get("qty_")) or 0) or None,
                                "weight": _r2(_pct(i.get("weights_"))), "month": i.get("ym_") or None})
        elif code == "OPTION":
            for i in items:
                kind = {"C": "買權", "P": "賣權"}.get(str(i.get("ename_") or "").strip(), "")
                options.append({"code": str(i.get("code_") or "").strip(),
                                "name": (str(i.get("name_") or "").strip() + (f"（{kind}）" if kind else "")),
                                "contracts": int(_num(i.get("qty_")) or 0) or None,
                                "weight": _r2(_pct(i.get("weights_"))), "month": i.get("ym_") or None})
        elif code == "MARGIN":
            kw["margin_pct"] = sum(w(i) or 0 for i in items)
        elif code == "CASH":
            for i in items:
                t = str(i.get("invtp_") or "")
                if t == "CASH" or "現金" in str(i.get("name_")):
                    kw["cash_pct"] = (kw.get("cash_pct") or 0) + (w(i) or 0)
                    kw["cash"] = (kw.get("cash") or 0) + (_num(i.get("amount_")) or 0)
                elif "應收" in str(i.get("name_")) or "應付" in str(i.get("name_")) or t == "PAYRCIV":
                    kw["recv_pct"] = (kw.get("recv_pct") or 0) + (w(i) or 0)
                elif "附買回" in str(i.get("name_")):
                    kw["repo_pct"] = (kw.get("repo_pct") or 0) + (w(i) or 0)
        elif code in ("REPO", "RP"):
            kw["repo_pct"] = (kw.get("repo_pct") or 0) + sum(w(i) or 0 for i in items)
    return make_alloc(net, "ctbc", data_date, futures=futures, options=options or None, **kw)


# ---- 富邦 Assets.aspx ----
FUBON_URL = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId={code}"


def parse_fubon(html):
    L = _html_text_lines(html)
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{2}/\d{2})", html)
    data_date = m.group(1).replace("/", "-") if m else ""

    def after(label):
        for i, l in enumerate(L):
            if l == label and i + 1 < len(L):
                return L[i + 1], (L[i + 2] if i + 2 < len(L) else None)
        return None, None

    net = _num(after("基金淨資產(新台幣)")[0])
    if not net:
        return {}
    kw = {}
    a, p = after("股票合計")
    if a is not None:
        kw["stock"], kw["stock_pct"] = _num(a), _pct(p)
    futures = []
    for i, l in enumerate(L):
        if i + 1 >= len(L):
            break
        v = _num(L[i + 1])
        if v is None:
            continue
        if l.startswith("現金"):
            kw["cash"] = (kw.get("cash") or 0) + v
        elif l.startswith("保證金"):
            kw["margin"] = (kw.get("margin") or 0) + v
        elif l.startswith("應收") or l.startswith("應付"):
            kw["recv"] = (kw.get("recv") or 0) + v
        elif l.startswith("附買回"):
            kw["repo"] = (kw.get("repo") or 0) + v
        elif l.startswith("期貨合計"):
            kw["fut_notional"] = v
    return make_alloc(net, "fubon", data_date, futures=futures, **kw)


def fetch_fubon(code, data_date=""):
    return parse_fubon(_get(FUBON_URL.format(code=code)).decode("utf-8", errors="replace"))


# ---- 復華 /api/assets ----
FH_API = "https://www.fhtrust.com.tw/api/assets?fundID={fid}&qDate={d}"


def parse_fhtrust(res):
    """result[0]：pcf_FundNav、result[{ftype,itemName,tot_mvalue}]、summary、rate[{i_cur,price(每 1 USD)}]。
    其他資產的金額是**原幣**（JPY/USD…），用 rate 表換成 NTD（實測可完全對上官網的其他資產 totRatio）。"""
    net = _num(res.get("pcf_FundNav"))
    data_date = str(res.get("dDate") or "").replace("/", "-")
    if not net:
        return {}
    per_usd = {str(r.get("i_cur")).upper(): _num(r.get("price")) for r in res.get("rate") or []}
    per_usd.setdefault("USD", 1.0)
    ntd_per_usd = per_usd.get("NTD") or per_usd.get("TWD")

    def to_ntd(amt, ccy):
        ccy = (ccy or "NTD").upper()
        if ccy in ("NTD", "TWD"):
            return amt
        if ntd_per_usd and per_usd.get(ccy):
            return amt / per_usd[ccy] * ntd_per_usd
        fx = _fx_to_twd(ccy)
        return amt * fx if fx else None

    kw = {}
    for s in res.get("summary") or []:
        if s.get("ftype") == "股票":
            kw["stock_pct"] = _pct(s.get("totRatio"))
    for r in res.get("result") or []:
        name = str(r.get("itemName") or "")
        amt = _num(r.get("tot_mvalue"))
        if amt is None:
            continue
        m = re.search(r"\(([A-Z]{3})\)", name)
        v = to_ntd(amt, m.group(1) if m else "NTD")
        if v is None:
            continue
        if r.get("ftype") == "股票":
            kw["stock"] = v
        elif "現金" in name:
            kw["cash"] = (kw.get("cash") or 0) + v
        elif "保證金" in name:
            kw["margin"] = (kw.get("margin") or 0) + v
        elif "應付" in name or "應收" in name:
            kw["recv"] = (kw.get("recv") or 0) + v
        elif "附買回" in name:
            kw["repo"] = (kw.get("repo") or 0) + v
    return make_alloc(net, "fhtrust", data_date, **kw)


def fetch_fhtrust(fund_id, data_date):
    url = FH_API.format(fid=fund_id, d=data_date.replace("-", "/"))
    res = (_get_json(url, {"Referer": f"https://www.fhtrust.com.tw/ETF/etf_detail/{fund_id}"}).get("result") or [{}])[0]
    a = parse_fhtrust(res)
    return a if a.get("dataDate") == data_date else {}


# ---- 群益 xlsx「其他資產」 ----

def parse_capital_xlsx(path, holdings=None):
    import pandas as pd
    x = pd.ExcelFile(path)
    net = None
    df0 = pd.read_excel(x, sheet_name=0, header=None, dtype=str)
    for _, r in df0.iterrows():
        if "淨資產" in str(r.iloc[0]):
            net = _num(r.iloc[1])
            break
    if not net:
        return {}
    kw = {}
    # 股票：股票 sheet 權重加總
    if len(x.sheet_names) > 1:
        dfs = pd.read_excel(x, sheet_name=1, header=0, dtype=str)
        wcol = next((c for c in dfs.columns if "權重" in str(c)), None)
        if wcol is not None:
            kw["stock_pct"] = sum(_pct(v) or 0 for v in dfs[wcol] if isinstance(v, str))
    other = next((s for s in x.sheet_names if "其他" in s), None)
    if other:
        dfo = pd.read_excel(x, sheet_name=other, header=None, dtype=str)
        for _, r in dfo.iterrows():
            label, val = str(r.iloc[0]).strip(), str(r.iloc[1]) if len(r) > 1 else ""
            m = re.match(r"\s*([A-Z]{3})", val)
            ccy = m.group(1) if m else "TWD"
            amt = _num(val[m.end():] if m else val)
            if amt is None:
                continue
            fx = _fx_to_twd(ccy)
            if fx is None:
                log.warning(f"群益其他資產 {label} {ccy} 無匯率，略過")
                continue
            v = amt * fx
            if "現金" in label:
                kw["cash"] = (kw.get("cash") or 0) + v
            elif "保證金" in label:
                kw["margin"] = (kw.get("margin") or 0) + v
            elif "應收" in label or "應付" in label:
                kw["recv"] = (kw.get("recv") or 0) + v
            elif "附買回" in label:
                kw["repo"] = (kw.get("repo") or 0) + v
    return make_alloc(net, "capital", **kw)


# ---- 台新 ETFSeriesDetail ----
TSIT_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/{code}"


def parse_tsit(html):
    L = _html_text_lines(html)
    net = None
    for i, l in enumerate(L):
        if "基金淨資產價值" in l and i + 1 < len(L):
            net = _num(L[i + 1])
            break
    if not net:
        return {}
    kw, futures = {}, []
    for i, l in enumerate(L):
        if l == "股票合計" and i + 1 < len(L):
            kw["stock_pct"] = _pct(L[i + 1])
        elif l == "流動比率(%)" and i + 2 < len(L):
            kw["cash"], kw["cash_pct"] = _num(L[i + 1]), _num(L[i + 2])
        elif l == "期貨名稱":
            # 表頭：期貨 | 期貨名稱 | 契約年月 | 口數 | 持股權重 | (名稱,年月,口數,權重)* | 期貨合計 | x%
            j = i + 4
            while j + 3 < len(L) and L[j] != "期貨合計":
                futures.append({"name": L[j], "code": L[j].split()[0], "month": L[j + 1],
                                "contracts": int(_num(L[j + 2]) or 0) or None, "weight": _pct(L[j + 3])})
                j += 4
    return make_alloc(net, "tsit", futures=futures, **kw)


def fetch_tsit(code, data_date=""):
    return parse_tsit(_get(TSIT_URL.format(code=code)).decode("utf-8", errors="replace"))


# ---- 元大 __NUXT__ ----

def parse_yuanta(extra):
    """extra = {"summary": FundWeights.Summary, "cash": Cash.CashPosition, "futures": FundWeights.FutureWeights}。"""
    s = (extra or {}).get("summary") or {}
    net = _num(s.get("fundsize"))
    if not net:
        return {}
    kw = {"stock": _num(s.get("stkvalues"))}
    for c in (extra or {}).get("cash") or []:
        name, rto = str(c.get("name") or ""), _num(c.get("rto"))
        if rto is None:
            continue
        if "現金" in name or "存款" in name:
            kw["cash_pct"] = (kw.get("cash_pct") or 0) + rto
        elif "保證金" in name:
            kw["margin_pct"] = (kw.get("margin_pct") or 0) + rto
        elif "附買回" in name:
            kw["repo_pct"] = (kw.get("repo_pct") or 0) + rto
        else:                                   # 應收股利、應付買回款、應收付證券款…
            kw["recv_pct"] = (kw.get("recv_pct") or 0) + rto
    futures = [{"code": str(f.get("code") or "").strip(), "name": str(f.get("name") or "").strip(),
                "contracts": int(_num(f.get("qty")) or 0) or None, "weight": _num(f.get("weights")),
                "month": f.get("ym")} for f in (extra or {}).get("futures") or []]
    fut = _num(s.get("futvalues"))
    return make_alloc(net, "yuanta", fut_notional=fut or None, futures=futures, **kw)


# 給 00990A 的 Playwright page.evaluate 用：從 __NUXT__ 取出 Summary／CashPosition／FutureWeights
YUANTA_EXTRA_JS = r"""
() => {
  const n = window.__NUXT__; const out = {summary: null, cash: null, futures: null};
  const seen = new Set();
  (function walk(o) {
    if (!o || typeof o !== 'object' || seen.has(o)) return; seen.add(o);
    if (o.FundWeights && o.FundWeights.Summary && !out.summary) {
      out.summary = o.FundWeights.Summary; out.futures = o.FundWeights.FutureWeights || [];
    }
    if (o.Cash && Array.isArray(o.Cash.CashPosition) && !out.cash) out.cash = o.Cash.CashPosition;
    for (const k in o) walk(o[k]);
  })(n);
  return out;
}
"""


# ---- 兆豐 etf_product.aspx ----
MEGA_URL = "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id={id}"


def parse_mega(html):
    L = _html_text_lines(html)
    net = None
    for i, l in enumerate(L):
        if "淨資產" in l and i + 1 < len(L) and _num(L[i + 1]):
            net = _num(L[i + 1])
            break
    pairs = re.findall(r'class="fund-title">\s*([^<]+?)\s*</div>\s*<div class="fund-amount">\s*([^<]+?)\s*</div>', html)
    kw = {}
    for label, amt_s in pairs:
        amt = _num(amt_s)
        if amt is None:
            continue
        m = re.match(r"股票\s*\(\s*([\d.]+)\s*%\s*\)", label)
        if m:
            kw["stock"], kw["stock_pct"] = amt, float(m.group(1))
            if not net and float(m.group(1)):
                net = amt / (float(m.group(1)) / 100)
        elif "現金" in label or "存款" in label:
            kw["cash"] = (kw.get("cash") or 0) + amt
        elif "保證金" in label:
            kw["margin"] = (kw.get("margin") or 0) + amt
        elif "應收" in label or "應付" in label:
            kw["recv"] = (kw.get("recv") or 0) + amt
        elif "附買回" in label:
            kw["repo"] = (kw.get("repo") or 0) + amt
        elif label.startswith("期貨"):
            kw["fut_notional"] = amt
    if not net:
        return {}
    return make_alloc(net, "mega", **kw)


def fetch_mega(page_id, data_date=""):
    return parse_mega(_get(MEGA_URL.format(id=page_id)).decode("utf-8", errors="replace"))


# ---- 凱基：只有股票權重 → 推算 ----

def from_holdings_weights(holdings, source="weights"):
    ws = [h.get("weight") or 0 for h in holdings or [] if not (h.get("is_futures") or h.get("isFutures"))]
    if not ws:
        return {}
    return _estimate_cash(make_alloc(None, source, stock_pct=sum(ws)))


# ============================================================================
# 註冊表：ETF → 取得方式
#   kind="raw"     由腳本把已抓到的原始資料傳進來（raw=...）
#   kind="fetch"   這裡自己抓；dated=True 表示可指定日期（可補抓前一交易日當比較基準）
# ============================================================================

REGISTRY = {
    "00400A": ("fetch", lambda d: fetch_cathay("EA", d), True),
    "00402A": ("raw", parse_fundassets, False),
    "00412A": ("raw", parse_fundassets, False),
    "00984A": ("raw", parse_fundassets, False),
    "00993A": ("raw", parse_fundassets, False),
    "00405A": ("fetch", lambda d: fetch_fubon("00405A", d), False),
    "00406A": ("raw", parse_ctbc, False),
    "00995A": ("raw", parse_ctbc, False),
    "00407A": ("holdings", from_holdings_weights, False),
    "00409A": ("fetch", lambda d: fetch_fhtrust("ETF26", d), True),
    "00418A": ("fetch", lambda d: fetch_fhtrust("ETF27", d), True),
    "00991A": ("fetch", lambda d: fetch_fhtrust("ETF23", d), True),
    "00413A": ("fetch", lambda d: fetch_esun("54", d), False),
    "00415A": ("xlsx", parse_capital_xlsx, False),
    "00982A": ("xlsx", parse_capital_xlsx, False),
    "00992A": ("xlsx", parse_capital_xlsx, False),
    "00997A": ("xlsx", parse_capital_xlsx, False),
    "00416A": ("fetch", lambda d: fetch_tsit("00416A", d), False),
    "00987A": ("fetch", lambda d: fetch_tsit("00987A", d), False),
    "00980A": ("fetch", lambda d: fetch_nomura("00980A", d), True),
    "00985A": ("fetch", lambda d: fetch_nomura("00985A", d), True),
    "00999A": ("fetch", lambda d: fetch_nomura("00999A", d), True),
    "00990A": ("raw", parse_yuanta, False),
    "00996A": ("fetch", lambda d: fetch_mega("23", d), False),
}


def _snap_path(code, d):
    return os.path.join(HOLDINGS_DIR, f"{code}_alloc_{d}.json")


def _save_snap(code, d, alloc):
    snap = {k: v for k, v in alloc.items() if k != "delta"}
    os.makedirs(HOLDINGS_DIR, exist_ok=True)
    with open(_snap_path(code, d), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)


def _load_prev_snap(code, d):
    pat = os.path.join(HOLDINGS_DIR, f"{code}_alloc_*.json")
    dated = []
    for f in glob.glob(pat):
        m = re.search(r"_alloc_(\d{4}-\d{2}-\d{2})\.json$", f)
        if m and m.group(1) < d:
            dated.append((m.group(1), f))
    if not dated:
        return None, {}
    pd_, f = max(dated)
    try:
        with open(f, encoding="utf-8") as fh:
            return pd_, json.load(fh)
    except Exception:
        return pd_, {}


def get_alloc(code, data_date, raw=None, holdings=None, xlsx_path=None):
    """取得 code 在 data_date 的資產配置（含 delta），並存快照。任何錯誤都回傳 {}。"""
    try:
        from asset_allocation import attach_delta
        entry = REGISTRY.get(code)
        if not entry or not data_date:
            return {}
        kind, fn, dated = entry
        if kind == "raw":
            alloc = fn(raw) if raw else {}
        elif kind == "holdings":
            alloc = fn(holdings)
        elif kind == "xlsx":
            path = xlsx_path or os.path.join(HOLDINGS_DIR, f"{code}_holdings_{data_date}.xlsx")
            alloc = fn(path) if os.path.exists(path) else {}
        else:
            alloc = fn(data_date)
        if not alloc or alloc.get("stockPct") is None:
            log.info(f"{code} 資產配置：來源無資料")
            return {}
        src_date = alloc.get("dataDate")
        if src_date and src_date != data_date:
            # 來源日期與持股日不同（例如安聯 NavDate＝T-1 而持股日用 PCF 推算）：照用但記錄
            log.info(f"{code} 資產配置來源日期 {src_date} ≠ 持股日 {data_date}")
        alloc["dataDate"] = data_date

        prev_d, prev = _load_prev_snap(code, data_date)
        if not prev and kind == "fetch" and dated:
            try:
                from tw_calendar import prev_trading_day
                pdate = prev_trading_day(datetime.strptime(data_date, "%Y-%m-%d").date()).strftime("%Y-%m-%d")
                prev = fn(pdate) or {}
                if prev.get("stockPct") is not None:
                    prev["dataDate"] = pdate
                    _save_snap(code, pdate, prev)
            except Exception as e:
                log.info(f"{code} 補抓前一交易日資產配置失敗：{e}")
                prev = {}
        attach_delta(alloc, prev)
        _save_snap(code, data_date, alloc)
        log.info(f"{code} 資產配置：股票 {alloc.get('stockPct')}%｜現金 {alloc.get('cashPct')}%"
                 f"{'(推算)' if alloc.get('cashEstimated') else ''}｜期貨 {alloc.get('futuresNotionalPct')}")
        return alloc
    except Exception as e:
        log.warning(f"{code} 資產配置取得失敗（不影響持股更新）：{e}")
        return {}
