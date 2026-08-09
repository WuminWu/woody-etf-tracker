"""
export_history.py
------------------
從 Google Sheets `holdings` 工作表讀取所有加減碼紀錄，
過濾出 diffShares != 0 的列，產出 history.json 供前端使用。

輸出格式（精簡為陣列，減少檔案大小）：
{
  "00981A": {
    "2330": [
      ["2026-05-11", -50000, -111750000.0],
      ["2026-05-08",  30000,   66900000.0]
    ],
    ...
  },
  ...
}

陣列順序：[日期, 加減碼股數, 加減碼金額]，依日期降序排列（最新在前）。
"""

import json
import logging
import os
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_ID = "1Wrz6y-DJSTM0oWMa0JRztryEiPwLB8QRU8HJYxHra00"
SHEET_TAB = "holdings"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
OUTPUT_FILE = "history.json"


def _get_service():
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json:
        log.warning("GOOGLE_SHEETS_CREDENTIALS not set — cannot export.")
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.warning(f"Google Sheets auth failed: {e}")
        return None


def main():
    service = _get_service()
    if service is None:
        return

    log.info("Fetching holdings sheet...")
    try:
        # Cols: A=日期 B=ETF代號 C=股票代號 D=股票名稱 E=股數 F=今日比例 G=昨日比例 H=股價 I=加減碼股數 J=加減碼金額
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:J",
        ).execute()
        rows = result.get("values", [])
    except Exception as e:
        log.error(f"Failed to read sheet: {e}")
        return

    if not rows or len(rows) < 2:
        log.warning("No data rows found.")
        return

    # history[etf_code][stock_code] = list of [date, diffShares, diffAmount]
    history = defaultdict(lambda: defaultdict(list))
    skipped = 0
    kept = 0

    for row in rows[1:]:  # skip header
        # Sheets API 省略 row 尾端空值，補齊到 10 欄
        padded = row + [""] * (10 - len(row))
        if len(padded) < 4 or not padded[0] or not padded[1] or not padded[2]:
            skipped += 1
            continue
        date, etf, stock_code, _name, _shares, _tw, _yw, _price, diff_shares, diff_amount = padded[:10]
        try:
            ds = int(float(diff_shares)) if diff_shares not in ("", None) else 0
        except (ValueError, TypeError):
            ds = 0
        if ds == 0:
            skipped += 1
            continue
        try:
            da = float(diff_amount) if diff_amount not in ("", None) else 0.0
        except (ValueError, TypeError):
            da = 0.0
        history[etf][stock_code].append([date, ds, round(da, 2)])
        kept += 1

    # 依日期降序排列（最新在前）
    for etf in history:
        for code in history[etf]:
            history[etf][code].sort(key=lambda r: r[0], reverse=True)

    # 轉成一般 dict
    out = {etf: dict(stocks) for etf, stocks in history.items()}

    Path(OUTPUT_FILE).write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_kb = Path(OUTPUT_FILE).stat().st_size / 1024
    log.info(
        f"Wrote {OUTPUT_FILE}: {len(out)} ETFs, "
        f"{sum(len(s) for s in out.values())} stock entries, "
        f"{kept} rows kept / {skipped} skipped ({size_kb:.1f} KB)."
    )


if __name__ == "__main__":
    main()
