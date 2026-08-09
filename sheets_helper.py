"""
Google Sheets helper — append daily ETF holdings to the master sheet.

Sheet: https://docs.google.com/spreadsheets/d/1Wrz6y-DJSTM0oWMa0JRztryEiPwLB8QRU8HJYxHra00
Tab:   holdings

Columns:
  A: 日期         YYYY-MM-DD
  B: ETF代號      e.g. 00981A
  C: 股票代號      e.g. 2330
  D: 股票名稱      e.g. 台積電
  E: 股數
  F: 持股比例(%)   todayWeight
  G: 前日比例(%)   yestWeight
  H: 股價
  I: 加減碼股數    diffShares
  J: 加減碼金額    diffAmount
  K: 基金市值(億)  totalMarketCap  (fund-level, same for all rows of same ETF+date)
  L: 基金股數(張)  totalShares     (fund-level, same for all rows of same ETF+date)
"""

import json
import os
import logging

log = logging.getLogger(__name__)

SHEET_ID   = "1Wrz6y-DJSTM0oWMa0JRztryEiPwLB8QRU8HJYxHra00"
SHEET_TAB  = "holdings"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER_ROW = ["日期", "ETF代號", "股票代號", "股票名稱",
               "股數", "持股比例(%)", "前日比例(%)",
               "股價", "加減碼股數", "加減碼金額",
               "基金市值(億)", "基金股數(張)"]


def _get_service():
    """Build and return a Google Sheets service object, or None on failure."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json:
        log.warning("GOOGLE_SHEETS_CREDENTIALS not set — skipping Sheets update.")
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(
            json.loads(creds_json), scopes=SCOPES
        )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.warning(f"Google Sheets auth failed: {e}")
        return None


def _get_sheet_id(service):
    """Return the numeric sheetId of the holdings tab."""
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            return s["properties"]["sheetId"]
    return None


def _ensure_tab_and_header(service):
    """Create the 'holdings' tab if missing; add/update header row."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        existing = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if SHEET_TAB not in existing:
            service.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]}
            ).execute()
            log.info(f"Created sheet tab: {SHEET_TAB}")
            _write_header(service)
            return

        # Tab exists — check header column count
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1:Z1"
        ).execute()
        current_header = result.get("values", [[]])[0] if result.get("values") else []

        if not current_header:
            _write_header(service)
        elif len(current_header) < len(HEADER_ROW):
            # Old header (e.g. 10 cols) → upgrade to new header (12 cols)
            _write_header(service)
            log.info(f"Header upgraded from {len(current_header)} to {len(HEADER_ROW)} columns.")
    except Exception as e:
        log.warning(f"Tab/header setup failed: {e}")


def _write_header(service):
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [HEADER_ROW]}
    ).execute()
    log.info("Header row written.")


def _already_exists(service, etf_code, data_date):
    """Return True if rows for this ETF + date already exist in the sheet."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:B"
        ).execute()
        rows = result.get("values", [])
        for row in rows[1:]:  # skip header
            if len(row) >= 2 and row[0] == data_date and row[1] == etf_code:
                return True
    except Exception as e:
        log.warning(f"Duplicate check failed: {e}")
    return False


def delete_duplicate_rows():
    """
    Scan the entire sheet and delete duplicate rows.

    A duplicate is defined as having the same (日期, ETF代號, 股票代號) — columns A+B+C.
    The FIRST occurrence is kept; all subsequent duplicates are deleted.

    Returns the number of rows deleted.
    """
    service = _get_service()
    if service is None:
        return 0

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:C"
        ).execute()
        rows = result.get("values", [])
    except Exception as e:
        log.warning(f"Failed to read sheet for dedup: {e}")
        return 0

    seen = set()
    duplicate_indices = []  # 0-based row indices

    for i, row in enumerate(rows):
        if i == 0:  # skip header row
            continue
        key = (
            row[0] if len(row) > 0 else "",
            row[1] if len(row) > 1 else "",
            row[2] if len(row) > 2 else "",
        )
        if key in seen:
            duplicate_indices.append(i)
        else:
            seen.add(key)

    if not duplicate_indices:
        log.info("Dedup scan: no duplicate rows found.")
        return 0

    log.info(f"Dedup scan: found {len(duplicate_indices)} duplicate rows — deleting...")

    sheet_id = _get_sheet_id(service)
    if sheet_id is None:
        log.warning("Cannot find sheet tab to delete rows.")
        return 0

    # Delete from bottom up so indices stay valid
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_idx,
                    "endIndex": row_idx + 1,
                }
            }
        }
        for row_idx in sorted(duplicate_indices, reverse=True)
    ]

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": requests}
        ).execute()
        log.info(f"Dedup: deleted {len(duplicate_indices)} duplicate rows.")
    except Exception as e:
        log.warning(f"Dedup deletion failed: {e}")
        return 0

    return len(duplicate_indices)


def append_holdings_to_sheets(etf_code, data_date, holdings, meta=None):
    """
    Append today's holdings for one ETF to the Google Sheet.

    Parameters
    ----------
    etf_code  : str        e.g. "00981A"
    data_date : str        e.g. "2026-04-22"
    holdings  : list       wrapper["holdings"] from generate_data_json
    meta      : dict|None  wrapper["meta"] — used for 基金市值 and 基金股數 columns.
                           Pass None to leave those columns blank (backward-compatible).
    """
    service = _get_service()
    if service is None:
        return

    _ensure_tab_and_header(service)

    if _already_exists(service, etf_code, data_date):
        log.info(f"Sheets: {etf_code} {data_date} already exists — skipped.")
        return

    total_market_cap = round(meta.get("totalMarketCap", 0.0), 2) if meta else ""
    total_shares     = meta.get("totalShares", 0) if meta else ""

    rows = []
    for h in holdings:
        if h.get("todayWeight", 0) <= 0:
            continue  # skip fully exited positions
        rows.append([
            data_date,
            etf_code,
            h.get("code", ""),
            h.get("name", ""),
            h.get("shares", 0),
            h.get("todayWeight", 0.0),
            h.get("yestWeight", 0.0),
            h.get("price", 0.0),
            h.get("diffShares", 0),
            round(h.get("diffAmount", 0.0), 2),
            total_market_cap,
            total_shares,
        ])

    if not rows:
        log.warning(f"Sheets: no rows to append for {etf_code} {data_date}.")
        return

    try:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:L",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}
        ).execute()
        log.info(f"Sheets: appended {len(rows)} rows for {etf_code} {data_date} "
                 f"(市值={total_market_cap}億, 股數={total_shares}張).")
    except Exception as e:
        log.warning(f"Sheets append failed for {etf_code}: {e}")
