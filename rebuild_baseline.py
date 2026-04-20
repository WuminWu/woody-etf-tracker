"""
One-shot script: regenerate data_00981A.json using the correct 4/17 holdings file.
"""
import json
import os
import glob
import yfinance as yf
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Load 4/17 holdings (the correct one from XLSX download)
with open("holdings/00981A_holdings_2026-04-17.json", "r", encoding="utf-8") as f:
    today_holdings = json.load(f)

print(f"Loaded {len(today_holdings)} stocks from 4/17 holdings")
print(f"2330 check: {next((h for h in today_holdings if h['code']=='2330'), None)}")

def get_price(code):
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = yf.Ticker(f"{code}{suffix}")
            hist = ticker.history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except:
            pass
    return 0.0

# No previous day data (4/17 is our baseline), so diffShares = 0 for all
final_output = []
total = len(today_holdings)
for i, h in enumerate(today_holdings):
    code = h["code"]
    name = h["name"]
    shares = int(str(h["shares"]).replace(",", ""))
    weight = float(str(h["weight"]).replace("%", "")) if isinstance(h["weight"], str) else h["weight"]

    price = get_price(code)
    print(f"  [{i+1}/{total}] {code} {name}: {shares} shares, {weight}%, price={price}")

    final_output.append({
        "code": code,
        "name": name,
        "shares": shares,
        "price": round(price, 2),
        "yestWeight": 0.0,   # No previous day data for baseline
        "todayWeight": weight,
        "diffShares": 0,
        "diffAmount": 0.0,
        "rank": i + 1,
    })

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
        "dataDate": "2026-04-17",
        "lastUpdate": datetime.now().strftime("%Y-%m-%d %H:%M"),
    },
    "holdings": final_output,
}

with open("data_00981A.json", "w", encoding="utf-8") as f:
    json.dump(wrapper, f, ensure_ascii=False, indent=4)

print(f"\nDone! data_00981A.json regenerated with correct 4/17 data.")
print(f"2330 in output: {next((h for h in final_output if h['code']=='2330'), None)}")
