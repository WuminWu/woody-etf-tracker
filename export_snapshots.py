"""
export_snapshots.py
--------------------
從 Google Sheets `holdings` 工作表讀取所有歷史資料，
產出每日 snapshot JSON 與 manifest，供前端歷史日期選擇器使用。

輸出檔案：
  snapshots/{YYYY-MM-DD}.json
    {
      "00981A": {
        "meta": { "dataDate": "...", "totalShares": ..., "totalMarketCap": ... },
        "holdings": [
          { "code": "2330", "name": "台積電",
            "shares": ..., "prevShares": ...,
            "yestWeight": ..., "todayWeight": ...,
            "price": ..., "diffShares": ..., "diffAmount": ...,
            "rank": 1 }
        ]
      },
      ...
    }

  snapshots/manifest.json
    {
      "dates": ["2026-04-22", "2026-04-23", ...],          # 全域有任何 ETF 資料的日期
      "etfs": {
        "00981A": ["2026-04-22", "2026-04-23", ...],       # 每檔 ETF 各自有資料的日期
        ...
      }
    }
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
OUTPUT_DIR = Path("snapshots")


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


def _to_float(v, default=0.0):
    try:
        return float(v) if v not in ("", None) else default
    except (ValueError, TypeError):
        return default


def _to_int(v, default=0):
    try:
        return int(float(v)) if v not in ("", None) else default
    except (ValueError, TypeError):
        return default


def fetch_sheet_rows(service):
    """Read all rows from holdings sheet. Returns list of dict per row."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A:L",
    ).execute()
    rows = result.get("values", [])
    if not rows or len(rows) < 2:
        return []

    parsed = []
    for r in rows[1:]:
        padded = r + [""] * (12 - len(r))
        if len(padded) < 4 or not padded[0] or not padded[1] or not padded[2]:
            continue
        parsed.append({
            "date": padded[0],
            "etf": padded[1],
            "code": padded[2],
            "name": padded[3],
            "shares": _to_int(padded[4]),
            "todayWeight": _to_float(padded[5]),
            "yestWeight": _to_float(padded[6]),
            "price": _to_float(padded[7]),
            "diffShares": _to_int(padded[8]),
            "diffAmount": _to_float(padded[9]),
            "totalMarketCap": _to_float(padded[10]) if padded[10] else None,
            "totalShares": _to_int(padded[11]) if padded[11] else None,
        })
    return parsed


def build_snapshots(rows):
    """
    分組成 { date: { etf: [holdings...] } } 結構。
    並把 prevShares 由 (shares - diffShares) 反推。
    """
    grouped = defaultdict(lambda: defaultdict(list))
    for r in rows:
        grouped[r["date"]][r["etf"]].append(r)

    snapshots = {}  # date -> etf -> {meta, holdings}
    for date in sorted(grouped.keys()):
        snapshots[date] = {}
        for etf, items in grouped[date].items():
            # 排序 by todayWeight desc
            items_sorted = sorted(items, key=lambda x: -x["todayWeight"])

            # meta：取第一個 row 的 fund-level 欄位
            meta_market_cap = next((it["totalMarketCap"] for it in items if it["totalMarketCap"] is not None), 0.0)
            meta_total_shares = next((it["totalShares"] for it in items if it["totalShares"] is not None), 0)

            holdings = []
            for rank, it in enumerate(items_sorted, 1):
                prev_shares = it["shares"] - it["diffShares"]
                holdings.append({
                    "code": it["code"],
                    "name": it["name"],
                    "shares": it["shares"],
                    "prevShares": prev_shares,
                    "price": it["price"],
                    "todayWeight": it["todayWeight"],
                    "yestWeight": it["yestWeight"],
                    "diffShares": it["diffShares"],
                    "diffAmount": round(it["diffAmount"], 2),
                    "rank": rank,
                })

            snapshots[date][etf] = {
                "meta": {
                    "dataDate": date,
                    "totalMarketCap": round(meta_market_cap, 2),
                    "totalShares": meta_total_shares,
                },
                "holdings": holdings,
            }

    return snapshots


def write_snapshots(snapshots):
    OUTPUT_DIR.mkdir(exist_ok=True)
    written = 0
    for date, etf_data in snapshots.items():
        out_path = OUTPUT_DIR / f"{date}.json"
        out_path.write_text(
            json.dumps(etf_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        written += 1
    log.info(f"Wrote {written} snapshot files to {OUTPUT_DIR}/")


def write_manifest(snapshots):
    all_dates = sorted(snapshots.keys())
    etf_dates = defaultdict(list)
    for date, etfs in snapshots.items():
        for etf_id in etfs.keys():
            etf_dates[etf_id].append(date)
    for etf_id in etf_dates:
        etf_dates[etf_id].sort()

    manifest = {
        "dates": all_dates,
        "etfs": dict(etf_dates),
    }
    out_path = OUTPUT_DIR / "manifest.json"
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    log.info(
        f"Wrote manifest: {len(all_dates)} dates, "
        f"{len(etf_dates)} ETFs ({sum(len(v) for v in etf_dates.values())} ETF-date entries)."
    )


def main():
    service = _get_service()
    if service is None:
        return
    log.info("Fetching holdings sheet...")
    rows = fetch_sheet_rows(service)
    log.info(f"Got {len(rows)} valid rows.")
    if not rows:
        return
    snapshots = build_snapshots(rows)
    write_snapshots(snapshots)
    write_manifest(snapshots)


if __name__ == "__main__":
    main()
