# -*- coding: utf-8 -*-
"""
backfill_gap.py — 補回「某一交易日」遺失的持股（群益系：來源仍可查歷史日期）。

與 backfill_day.py 的差異：backfill_day 會跑完整 main()，把 data_*.json 覆寫成舊日期，
只適合補「最近一天」。本工具專補更早的缺口：只寫 holdings/{code}_holdings_{date}.json
並補上 Google 試算表的列，**不動 data_*.json、不發 Telegram**。

股價用該日的實際收盤（auto_adjust=False，還原當時記錄的數字，不套用之後的配息調整）。

用法：
    python backfill_gap.py 00992A 2026-06-18            # 乾跑，只印不寫
    python backfill_gap.py 00992A 2026-06-18 --apply    # 實際寫入
"""
import importlib
import json
import os
import sys

import pandas as pd
import yfinance as yf

from tw_calendar import next_trading_day, prev_trading_day
from sheets_helper import append_holdings_to_sheets

os.chdir(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_DIR = "holdings"


def hist_closes(codes, day):
    """回傳 {code: 該日收盤（台幣、未還原配息）}。台股試 .TW/.TWO；
    海外用 market_utils 轉 yfinance 代號，再乘以該日匯率換成台幣。"""
    from market_utils import yf_symbol, ccy_of
    tw, ov = {}, {}
    for c in codes:
        parts = c.split()
        if len(parts) == 1:
            tw[f"{parts[0]}.TW"] = c
            tw[f"{parts[0]}.TWO"] = c
        else:
            ov[yf_symbol(parts[0], parts[1])] = (c, ccy_of(parts[1]))

    def grab(tickers, start, end):
        """{ticker: 最後一筆收盤}"""
        got = {}
        if not tickers:
            return got
        df = yf.download(list(tickers), start=start, end=end, progress=False,
                         group_by="ticker", auto_adjust=False, threads=True)
        for t in tickers:
            try:
                col = df[t]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
                col = col.dropna()
                if len(col):
                    got[t] = float(col.iloc[-1])
            except Exception:
                pass
        return got

    day_end = day + pd.Timedelta(days=1)
    out = {}
    for t, px in grab(list(tw) + list(ov), day, day_end).items():
        if t in tw:
            out.setdefault(tw[t], round(px, 2))
        else:
            out[t] = px   # 海外先存原幣，稍後換匯

    # 該日匯率（當日可能無報價，往前抓幾天取最近一筆）
    ccys = {c for _, c in ov.values()} - {"TWD"}
    fx = {}
    if ccys:
        rates = grab([f"{c}TWD=X" for c in ccys], day - pd.Timedelta(days=7), day_end)
        fx = {c: rates.get(f"{c}TWD=X", 0.0) for c in ccys}
    for t, (code, ccy) in ov.items():
        if t in out:
            r = 1.0 if ccy == "TWD" else fx.get(ccy, 0.0)
            v = out.pop(t) * r
            if r > 0:
                out[code] = round(v, 2)
    return out


def backfill(code, date_str, apply=False):
    import datetime as dt
    day = dt.date.fromisoformat(date_str)
    mod = importlib.import_module(f"check_and_update_{code}")
    form = next_trading_day(day).strftime("%Y/%m/%d")
    prev_day = prev_trading_day(day).strftime("%Y-%m-%d")

    print(f"\n=== {code} {date_str}（查詢日 {form}；前一交易日 {prev_day}）===")
    out_json = os.path.join(HOLDINGS_DIR, f"{code}_holdings_{date_str}.json")
    if os.path.exists(out_json):
        print("  已存在，跳過"); return

    path = mod.download_xlsx(form)
    if not path or not os.path.exists(path):
        print("  下載失敗（來源可能已無此日期）"); return
    try:
        holdings = mod.parse_holdings_from_xlsx(path)
        aum_ntd, units = mod.parse_aum_from_xlsx(path)
    finally:
        if os.path.exists(path):
            os.remove(path)
    if not holdings:
        print("  解析不到持股"); return

    prev_path = os.path.join(HOLDINGS_DIR, f"{code}_holdings_{prev_day}.json")
    prev = {h["code"]: h for h in json.load(open(prev_path, encoding="utf-8"))} if os.path.exists(prev_path) else {}
    print(f"  持股 {len(holdings)} 檔；AUM {aum_ntd/1e8:.2f}億；單位數 {units:,}；前一日基準 {'有' if prev else '無'}（{len(prev)} 檔）")

    prices = hist_closes([h["code"] for h in holdings], day)
    rows = []
    for h in holdings:
        pv = prev.get(h["code"], {})
        diff = h["shares"] - pv.get("shares", 0)
        px = prices.get(h["code"], 0.0)
        rows.append({"code": h["code"], "name": h["name"], "shares": h["shares"],
                     "prevShares": pv.get("shares", 0), "price": px, "prevPrice": 0,
                     "yestWeight": pv.get("weight", 0.0), "todayWeight": h["weight"],
                     "diffShares": diff, "diffAmount": round(diff * px, 2)})
    rows.sort(key=lambda x: -x["todayWeight"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    meta = {"dataDate": date_str, "totalShares": units // 1000 if units else 0,
            "totalMarketCap": round(aum_ntd / 1e8, 2) if aum_ntd else 0.0}
    got = sum(1 for r in rows if r["price"])
    print(f"  取得當日收盤 {got}/{len(rows)} 檔")
    for r in rows[:3]:
        print(f"    {r['code']} {r['name'][:8]:<8} 股數 {r['shares']:>10,} 前日 {r['prevShares']:>10,} "
              f"diff {r['diffShares']:>+9,} 價 {r['price']:>8,.2f} 金額 {r['diffAmount']:>+14,.0f}")

    if not apply:
        print("  [乾跑] 未寫入任何檔案／試算表")
        return
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)
    append_holdings_to_sheets(code, date_str, rows, meta=meta)
    print(f"  [已寫入] {out_json} + 試算表 {len(rows)} 列")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    backfill(args[0], args[1], apply="--apply" in sys.argv)
