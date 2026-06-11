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

# 新發行 ETF 的 YTD 應以 IPO 掛牌價為基準（yfinance 在上市初期回傳的「年內第一筆」
# 不見得是 IPO 價，會造成 YTD 失真）。掛牌當年沿用此設定，跨年後自然失效。
IPO_BASELINE = {
    # code: (ipo_date YYYY-MM-DD, ipo_price)
    "00403A": ("2026-05-12", 10.0),
}


def get_ipo_baseline(code):
    """若 code 屬於 IPO 當年，回傳 (date, price)；否則 None。"""
    if code not in IPO_BASELINE:
        return None
    ipo_date, ipo_price = IPO_BASELINE[code]
    ipo_year = int(ipo_date.split("-")[0])
    now_year = datetime.now(timezone(timedelta(hours=8))).year
    return (ipo_date, ipo_price) if now_year == ipo_year else None


ETFS = [
    ("00981A", "data_00981A.json"),
    ("00403A", "data_00403A.json"),
    ("00988A", "data_00988A.json"),
    ("00980A", "data_00980A.json"),
    ("00985A", "data_00985A.json"),
    ("00991A", "data_00991A.json"),
    ("00992A", "data_00992A.json"),
    ("00982A", "data_00982A.json"),
    ("00987A", "data_00987A.json"),
    ("00993A", "data_00993A.json"),
    ("00995A", "data_00995A.json"),
    ("00996A", "data_00996A.json"),
]


def fetch_ytd_price(ticker_symbol, code=None):
    try:
        hist = yf.Ticker(ticker_symbol).history(period="ytd", timeout=10)
        if len(hist) >= 2:
            last = hist["Close"].iloc[-1]
            price = round(float(last), 2)

            # 若是 IPO 當年的新 ETF，以 IPO 掛牌價當 baseline
            baseline = get_ipo_baseline(code) if code else None
            if baseline:
                _, ipo_price = baseline
                ytd = f"{((float(last) - ipo_price) / ipo_price) * 100:.2f}"
            else:
                first = hist["Close"].iloc[0]
                ytd = f"{((float(last) - float(first)) / float(first)) * 100:.2f}"
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

        ytd, price = fetch_ytd_price(f"{code}.TW", code=code)
        if ytd is None:
            print(f"  {code}: no data, skipping")
            continue

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        data["meta"]["ytd"] = ytd
        data["meta"]["etfPrice"] = price
        data["meta"]["priceDate"] = now_str[:10]  # YYYY-MM-DD only
        # Update priceChange & prevPrice based on ytd history
        try:
            hist = yf.Ticker(f"{code}.TW").history(period="ytd", timeout=10)
            if len(hist) >= 2:
                prev_p = round(float(hist["Close"].iloc[-2]), 2)
                data["meta"]["prevPrice"] = prev_p
                data["meta"]["priceChange"] = round((price - prev_p) / prev_p * 100, 2)
        except Exception:
            pass
        # Recalculate totalMarketCap from existing totalShares × new price
        # (totalShares is only authoritative from official XLSX — do NOT overwrite it here)
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
