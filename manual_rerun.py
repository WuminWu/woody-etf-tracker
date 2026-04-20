"""
Manual rerun: force regenerate data_00981A.json from 4/20 holdings, compare with 4/17, push.
"""
import json
import os
import sys
import yfinance as yf
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from check_and_update import parse_holdings_from_xlsx, get_price, git_push

# Load 4/20 holdings
with open("holdings/00981A_holdings_2026-04-20.json", "r", encoding="utf-8") as f:
    today_h = json.load(f)

# Load 4/17 holdings  
with open("holdings/00981A_holdings_2026-04-17.json", "r", encoding="utf-8") as f:
    prev_h = json.load(f)

prev_dict = {h["code"]: h for h in prev_h}

print(f"Today: {len(today_h)} stocks, Prev: {len(prev_h)} stocks")

final_output = []
for i, h in enumerate(today_h):
    code = h["code"]
    name = h["name"]
    shares = int(str(h["shares"]).replace(",", ""))
    weight = float(str(h["weight"]).replace("%", "")) if isinstance(h["weight"], str) else h["weight"]

    prev_data = prev_dict.get(code, {})
    prev_shares = int(str(prev_data.get("shares", "0")).replace(",", "")) if prev_data else 0
    prev_weight = float(str(prev_data.get("weight", "0")).replace("%", "")) if prev_data and prev_data.get("weight") else 0.0

    diff_shares = shares - prev_shares
    price = get_price(code)
    diff_amount = diff_shares * price

    print(f"  [{i+1}/{len(today_h)}] {code} {name}: price={price}, diff={diff_shares}")

    final_output.append({
        "code": code,
        "name": name,
        "shares": shares,
        "price": round(price, 2),
        "yestWeight": prev_weight,
        "todayWeight": weight,
        "diffShares": diff_shares,
        "diffAmount": round(diff_amount, 2),
    })

# Stocks removed (in prev but not in today)
today_codes = {h["code"] for h in today_h}
for ph in prev_h:
    if ph["code"] not in today_codes:
        code = ph["code"]
        prev_shares = int(str(ph["shares"]).replace(",", ""))
        prev_weight = float(str(ph["weight"]).replace("%", "")) if isinstance(ph["weight"], str) else ph["weight"]
        price = get_price(code)
        print(f"  [REMOVED] {code} {ph['name']}: price={price}")
        final_output.append({
            "code": code,
            "name": ph["name"],
            "shares": 0,
            "price": round(price, 2),
            "yestWeight": prev_weight,
            "todayWeight": 0.0,
            "diffShares": -prev_shares,
            "diffAmount": round(-prev_shares * price, 2),
        })

# Sort by weight desc
final_output = sorted(final_output, key=lambda x: x["todayWeight"], reverse=True)
for idx, item in enumerate(final_output):
    item["rank"] = idx + 1

# YTD
ytd_val = "0.0"
try:
    ytd_hist = yf.Ticker("00981A.TW").history(period="ytd")
    if len(ytd_hist) >= 2:
        ytd_calc = ((ytd_hist["Close"].iloc[-1] - ytd_hist["Close"].iloc[0]) / ytd_hist["Close"].iloc[0]) * 100
        ytd_val = f"{ytd_calc:.2f}"
except:
    pass

wrapper = {
    "meta": {
        "manager": "陳釧瑤",
        "ytd": ytd_val,
        "dataDate": "2026-04-20",
        "lastUpdate": datetime.now().strftime("%Y-%m-%d %H:%M"),
    },
    "holdings": final_output,
}

with open("data_00981A.json", "w", encoding="utf-8") as f:
    json.dump(wrapper, f, ensure_ascii=False, indent=4)

print(f"\nDone! {len(final_output)} holdings written to data_00981A.json")
print(f"Pushing to GitHub...")
git_push()
