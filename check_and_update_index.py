"""
check_and_update_index.py
--------------------------
每日更新 data_index.json：
  加權指數 (^TWII) YTD 績效（網頁 header 顯示）
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf


def update_twii():
    twii_ytd = "0.00"
    try:
        hist_ytd = yf.Ticker("^TWII").history(period="ytd", timeout=15)
        if len(hist_ytd) >= 2:
            first = hist_ytd["Close"].iloc[0]
            last = hist_ytd["Close"].iloc[-1]
            twii_ytd = f"{((last - first) / first) * 100:.2f}"
            print(f"TWII YTD: {twii_ytd}%")
        else:
            print("Not enough historical data for ^TWII YTD")
    except Exception as e:
        print(f"Error fetching ^TWII: {e}", file=sys.stderr)

    data = {
        "twii_ytd": twii_ytd,
        "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
    }

    with open("data_index.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Saved data_index.json")


if __name__ == "__main__":
    update_twii()
