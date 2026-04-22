"""
Google Sheets helper — append daily ETF holdings to the master sheet.

Sheet: https://docs.google.com/spreadsheets/d/1oK8ICXl0euyxJocRxHLknzWD6bccukKoWQI_ke_XqA8
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
"""

import json
import os
import logging

log = logging.getLogger(__name__)

SHEET_ID   = "1oK8ICXl0euyxJocRxHLknzWD6bccukKoWQI_ke_XqA8"
SHEET_TAB  = "holdings"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER_ROW = ["日期", "ETF代號", "股票代號", "股票名稱",
               "股數", "持股比例(%)", "前日比例(%)",
               "股價", "加減碼股數", "加減碼金額"]


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


def _ensure_tab_and_header(service):
    """Create the 'holdings' tab if missing; add header row if sheet is empty."""
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

        # Tab exists — check if A1 has header
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1"
        ).execute()
        if not result.get("values"):
            _write_header(service)
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


def append_holdings_to_sheets(etf_code, data_date, holdings):
    """
    Append today's holdings for one ETF to the Google Sheet.

    Parameters
    ----------
    etf_code  : str   e.g. "00981A"
    data_date : str   e.g. "2026-04-22"
    holdings  : list  wrapper["holdings"] from generate_data_json
    """
    service = _get_service()
    if service is None:
        return

    _ensure_tab_and_header(service)

    if _already_exists(service, etf_code, data_date):
        log.info(f"Sheets: {etf_code} {data_date} already exists — skipped.")
        return

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
        ])

    if not rows:
        log.warning(f"Sheets: no rows to append for {etf_code} {data_date}.")
        return

    try:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:J",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}
        ).execute()
        log.info(f"Sheets: appended {len(rows)} rows for {etf_code} {data_date}.")
    except Exception as e:
        log.warning(f"Sheets append failed for {etf_code}: {e}")
