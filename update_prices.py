"""
Only updates etfPrice + ytd in data_*.json meta fields.
Holdings data is left unchanged.
Also refreshes data_index.json (TWII YTD).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

ETFS = [
    ("00981A", "data_00981A.json"),
    ("00980A", "data_00980A.json"),
    ("00985A", "data_00985A.json"),
    ("00991A", "data_00991A.json"),
    ("00992A", "data_00992A.json"),
    ("00982A", "data_00982A.json"),
    ("00987A", "data_00987A.json"),
    ("00993A", "data_00993A.json"),
    ("00995A", "data_00995A.json"),
]


def fetch_ytd_price(ticker_symbol):
    try:
        hist = yf.Ticker(ticker_symbol).history(period="ytd", timeout=10)
        if len(hist) >= 2:
            first = hist["Close"].iloc[0]
            last = hist["Close"].iloc[-1]
            ytd = f"{((last - first) / first) * 100:.2f}"
            price = round(float(last), 2)
            return ytd, price
    except Exception as e:
        print(f"  Warning: {ticker_symbol} fetch failed: {e}", file=sys.stderr)
    return None, None


def update_etf_prices():
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    updated = []

    for code, data_file in ETFS:
        path = Path(data_file)
        if not path.exists():
            print(f"  Skip {data_file} (not found)")
            continue

        ytd, price = fetch_ytd_price(f"{code}.TW")
        if ytd is None:
            print(f"  {code}: no data, skipping")
            continue

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["meta"]["ytd"] = ytd
        data["meta"]["etfPrice"] = price
        data["meta"]["lastUpdate"] = now_str
        # Update totalShares & totalMarketCap from yfinance totalAssets
        try:
            _info = yf.Ticker(f"{code}.TW").info
            _assets = float(_info.get("totalAssets") or 0)
            if _assets > 0 and price > 0:
                data["meta"]["totalShares"] = round(_assets / price) // 1000
                data["meta"]["totalMarketCap"] = round(_assets / 1e8, 2)
        except Exception:
            # Fallback: recalculate market cap from existing totalShares
            total_shares_zhang = data["meta"].get("totalShares") or 0
            if total_shares_zhang and price:
                data["meta"]["totalMarketCap"] = round(price * total_shares_zhang * 1000 / 1e8, 2)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

        print(f"  {code}: price={price}, ytd={ytd}%")
        updated.append(code)

    # Update TWII index
    twii_ytd, _ = fetch_ytd_price("^TWII")
    if twii_ytd is not None:
        index_data = {"twii_ytd": twii_ytd, "lastUpdate": now_str}
        with open("data_index.json", "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        print(f"  TWII: ytd={twii_ytd}%")

    print(f"Done. Updated: {updated}")


if __name__ == "__main__":
    update_etf_prices()
