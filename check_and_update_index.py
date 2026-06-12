"""
check_and_update_index.py
--------------------------
每日更新 data_index.json：
1. 加權指數 (^TWII) YTD 績效
2. 大盤指數收盤價/漲跌
3. 振幅統計（供「大盤振幅參考」面板使用）：
   - TR 真實波幅（含跳空）：max(高-低, |高-前收|, |低-前收|) / 前收
   - ATR5 / ATR10 / ATR20：短中期波動水位
   - 近 60 日 TR 分布百分位 P50 / P75 / P90 / P95：長期常態範圍
   - 全部換算為點數（以最新收盤為基準）
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf


def compute_amplitude_stats(hist):
    """
    hist: yfinance DataFrame (需含 High/Low/Close，依日期升冪)
    回傳 dict 或 None。
    """
    if len(hist) < 30:
        return None

    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    prev_close = close.shift(1)

    # True Range（百分比，相對前收）
    import pandas as pd
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr_pct = (tr / prev_close * 100).dropna()

    if len(tr_pct) < 30:
        return None

    last_close = float(close.iloc[-1])
    prev_c = float(close.iloc[-2]) if len(close) >= 2 else last_close

    # 今日（最新一筆）已實現 TR
    today_tr_pct = round(float(tr_pct.iloc[-1]), 2)
    today_date = hist.index[-1].strftime("%Y-%m-%d")

    def to_points(pct):
        return round(last_close * pct / 100)

    def pp(pct):
        pct = round(float(pct), 2)
        return {"pct": pct, "points": to_points(pct)}

    # 各天數窗口：TR 平均 + 標準差 → 平均+1σ/2σ/3σ
    # 注意：窗口不含今日（用 [-n-1:-1]），否則今天的大振幅會把自己的門檻拉高
    windows = {}
    for n in (5, 10, 20):
        w = tr_pct.iloc[-(n + 1):-1]
        if len(w) < n:
            continue
        mean = float(w.mean())
        std = float(w.std(ddof=1))
        windows[f"d{n}"] = {
            "mean":   pp(mean),
            "sigma1": pp(mean + std),
            "sigma2": pp(mean + 2 * std),
            "sigma3": pp(mean + 3 * std),
        }

    return {
        "dataDate": today_date,
        "close": round(last_close, 2),
        "change": round(last_close - prev_c, 2),
        "changePct": round((last_close - prev_c) / prev_c * 100, 2) if prev_c else 0,
        "todayTrPct": today_tr_pct,
        "todayTrPoints": to_points(today_tr_pct),
        "windows": windows,
    }


def update_twii():
    twii_ytd = "0.00"
    amplitude = None
    try:
        # ytd 用於績效；6mo 用於振幅統計（确保 >60 交易日）
        hist_ytd = yf.Ticker("^TWII").history(period="ytd", timeout=15)
        if len(hist_ytd) >= 2:
            first = hist_ytd["Close"].iloc[0]
            last = hist_ytd["Close"].iloc[-1]
            twii_ytd = f"{((last - first) / first) * 100:.2f}"
            print(f"TWII YTD: {twii_ytd}%")
        else:
            print("Not enough historical data for ^TWII YTD")

        hist_6mo = yf.Ticker("^TWII").history(period="6mo", timeout=15)
        amplitude = compute_amplitude_stats(hist_6mo)
        if amplitude:
            d10 = amplitude["windows"].get("d10", {})
            print(f"TWII close={amplitude['close']}, todayTR={amplitude['todayTrPct']}%, "
                  f"10日mean={d10.get('mean',{}).get('pct')}% +1σ={d10.get('sigma1',{}).get('pct')}%")
        else:
            print("Amplitude stats unavailable (insufficient data)")
    except Exception as e:
        print(f"Error fetching ^TWII: {e}", file=sys.stderr)

    data = {
        "twii_ytd": twii_ytd,
        "lastUpdate": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
    }
    if amplitude:
        data["amplitude"] = amplitude

    with open("data_index.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved data_index.json")


if __name__ == "__main__":
    update_twii()
