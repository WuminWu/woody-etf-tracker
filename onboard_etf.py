"""
onboard_etf.py
--------------
新 ETF 上線：用「基準日」持股一次建好網頁資料與成本價，掛牌當天就能在網頁看到持股與成本。

前置：
  1. etfs.json 已加上這檔（etf_registry）
  2. check_and_update_{代號}.py 已寫好（這裡借用它的 CFG）
  3. holdings/{代號}_holdings_{基準日}.json 已存好（跑爬蟲、或手動從官網存）

用法：
  python onboard_etf.py 00416A 2026-11-02 --aum 8586247465 --units 871097000
  python onboard_etf.py 00416A 2026-11-02 --dry-run      # 只印結果，不寫檔
  --no-cost   不建成本價      --force   覆寫既有的 data json／成本紀錄

做的事：
  - data_{代號}.json：dataDate＝基準日，個股價＝基準日證交所／櫃買官方收盤，
    「前一日」用同一份持股 → 無加減碼、不會整份顯示「新增」
  - cost_basis.json：每檔成本＝基準日官方收盤（since 基準日），_lastProcessed＝基準日，
    之後 update_cost_basis 照常處理下一個交易日（加碼加權平均、新建倉記當日收盤）
  - 不寫試算表、不發 Telegram；下一次排程就以這份持股當比較基準
海外代號（如 NVDA US）沒有官方台股收盤，改用 yfinance 最新價並列出提醒。
"""

import argparse
import importlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import etf_registry
import tw_prices
from etf_core import build_data_json, get_price
from update_cost_basis import load_cost_basis, save_cost_basis


def main():
    ap = argparse.ArgumentParser(description="新 ETF 上線：建基準日 data json 與成本價")
    ap.add_argument("code")
    ap.add_argument("date", help="基準日 YYYY-MM-DD（holdings 檔的日期）")
    ap.add_argument("--aum", type=int, default=0, help="基金淨資產（元）")
    ap.add_argument("--units", type=int, default=0, help="已發行單位數（同爬蟲傳給 build_data_json 的單位）")
    ap.add_argument("--no-cost", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not etf_registry.get(a.code):
        sys.exit(f"etfs.json 沒有 {a.code}，請先加上")
    script = f"check_and_update_{a.code}.py"
    if not os.path.exists(script):
        sys.exit(f"找不到 {script}")
    hpath = os.path.join("holdings", f"{a.code}_holdings_{a.date}.json")
    if not os.path.exists(hpath):
        sys.exit(f"找不到 {hpath}：先用爬蟲或手動存好基準日持股")
    holdings = json.load(open(hpath, encoding="utf-8"))

    data_file = f"data_{a.code}.json"
    if os.path.exists(data_file) and not a.force:
        old = json.load(open(data_file, encoding="utf-8")).get("meta", {}).get("dataDate", "")
        sys.exit(f"{data_file} 已存在（dataDate {old}），要覆寫請加 --force")

    closes = tw_prices.closes_for(a.date)
    if not closes:
        sys.exit(f"{a.date} 的官方收盤價還沒有（休市或尚未公布）")
    prices, overseas, missing = {}, [], []
    for h in holdings:
        c = str(h["code"])
        if c in closes:
            prices[c] = closes[c]
        elif " " in c:
            prices[c] = get_price(c)
            overseas.append(c)
        else:
            missing.append(c)
    if missing:
        sys.exit(f"這些台股代號在 {a.date} 查無官方收盤：{missing}")

    cfg = importlib.import_module(script[:-3]).CFG   # 借用爬蟲的設定（規模單位、期貨、yf 後綴…）
    os.chdir(ROOT)   # 爬蟲 import 時會 chdir 到自己的目錄（同一處，保險起見）

    print(f"{a.code} 基準日 {a.date}：{len(holdings)} 檔，股票市值 "
          f"{sum(h['shares'] * prices[str(h['code'])] for h in holdings) / 1e8:.2f} 億")
    if overseas:
        print(f"  海外代號用 yfinance 最新價（非基準日收盤）：{overseas}")
    if a.dry_run:
        for h in holdings[:10]:
            print(f"  {h['code']} {h['name']} {prices[str(h['code'])]}")
        print("（dry-run，未寫檔）")
        return

    w = build_data_json(cfg, holdings, holdings, a.date, aum_ntd=a.aum, units=a.units,
                        price_fn=lambda c: prices[str(c)])
    m = w["meta"]
    print(f"  已寫 {data_file}：規模 {m['totalMarketCap']} 億、{m['totalShares']:,} 張、ETF 價 {m['etfPrice']}")

    if a.no_cost:
        return
    if load_cost_basis().get(a.code) and not a.force:
        sys.exit(f"cost_basis.json 已有 {a.code} 的成本紀錄，要覆寫請加 --force")
    n = seed_cost_basis(a.code, a.date, holdings, lambda c: prices[str(c)])
    print(f"  已寫 cost_basis.json：{n} 檔，成本＝{a.date} 收盤，_lastProcessed＝{a.date}")


def seed_cost_basis(code, date_str, holdings, price_of):
    """以基準日收盤當成本，整組覆寫 cost_basis.json[code]，_lastProcessed＝基準日。回傳建檔檔數。

    也給「掛牌前沒揭露持股」的爬蟲在第一次拿到持股時呼叫（如 00418A），
    之後 update_cost_basis 看到 _lastProcessed 已是當天就跳過，隔天照常加權平均。
    """
    cb = load_cost_basis()
    cb[code] = {}
    for h in holdings:
        p = price_of(h["code"]) or 0
        if h.get("shares", 0) > 0 and p > 0:
            cb[code][str(h["code"])] = {"avgCost": round(p, 2), "shares": h["shares"], "since": date_str}
    cb.setdefault("_lastProcessed", {})[code] = date_str
    save_cost_basis(cb)
    return len(cb[code])


if __name__ == "__main__":
    main()
