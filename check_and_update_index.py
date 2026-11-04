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

    try:
        with open("data_index.json", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    # 抓不到就沿用舊值：原本會把網頁 header 的加權指數 YTD 蓋成 0.00（7/24 曾發生）
    if twii_ytd == "0.00" and prev.get("twii_ytd") not in (None, "0.00"):
        print(f"沿用上次的 TWII YTD：{prev['twii_ytd']}%")
        twii_ytd = prev["twii_ytd"]
    # 數值沒變就不改寫：原本每輪都更新 lastUpdate，收盤後資料不再變動，
    # 排程每 30 分鐘仍各產生一個只改時間的 commit（每晚多 6 個、Cloudflare 多部署 6 次）
    if prev.get("twii_ytd") == twii_ytd:
        print("data_index.json 無變動，不改寫")
        return

    data = {
        "twii_ytd": twii_ytd,
        "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
    }

    with open("data_index.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Saved data_index.json")


if __name__ == "__main__":
    update_twii()
