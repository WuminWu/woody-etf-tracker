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
import time

from sanity import holdings_count_check

log = logging.getLogger(__name__)

# 寫入失敗的 (ETF, 日期) 記在這裡，由 sheets_reconcile.py 在下一輪自動補寫。
# 原本失敗只記一行 warning：holdings 檔已存在，爬蟲之後每輪都直接跳過，
# 那一天就永遠不在試算表裡（00980A 2026-07-30 即因 HTTP 400 永久缺漏，
# 快照、歷史、週報全部少了那天）。
_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
PENDING_FILE = os.path.join(_STATE_DIR, "sheets_pending.json")
# 因「持股檔數驟降」被擋下的 (ETF, 日期)：只警告一次，對帳時也不再重試
BLOCKED_FILE = os.path.join(_STATE_DIR, "sheets_blocked.json")


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"讀取 {os.path.basename(path)} 失敗（{e}），視為空白")
        return {}


def _save_state(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _key(etf_code, data_date):
    return f"{etf_code}|{data_date}"


def pending_writes():
    """尚待補寫的 {"ETF|日期": {"error", "since"}}。"""
    return _load_state(PENDING_FILE)


def _set_pending(etf_code, data_date, error=None):
    """error=None 表示已寫入成功，從待補清單移除。回傳這筆是否原本就在清單上。"""
    state = pending_writes()
    k = _key(etf_code, data_date)
    existed = k in state
    if error is None:
        if existed:
            del state[k]
            _save_state(PENDING_FILE, state)
    else:
        state.setdefault(k, {"since": time.strftime("%Y-%m-%d %H:%M")})
        state[k]["error"] = str(error)[:300]
        _save_state(PENDING_FILE, state)
    return existed


def is_blocked(etf_code, data_date):
    return _key(etf_code, data_date) in _load_state(BLOCKED_FILE)


def _notify(message):
    try:
        from notify import send_telegram
        send_telegram(message)
    except Exception as e:
        log.warning(f"Telegram 通知失敗：{e}")

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
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute(num_retries=5)
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            return s["properties"]["sheetId"]
    return None


def _ensure_tab_and_header(service):
    """Create the 'holdings' tab if missing; add/update header row."""
    try:
        meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute(num_retries=5)
        existing = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if SHEET_TAB not in existing:
            service.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]}
            ).execute(num_retries=5)
            log.info(f"Created sheet tab: {SHEET_TAB}")
            _write_header(service)
            return

        # Tab exists — check header column count
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1:Z1"
        ).execute(num_retries=5)
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
    ).execute(num_retries=5)
    log.info("Header row written.")


def _already_exists(service, etf_code, data_date):
    """Return True if rows for this ETF + date already exist in the sheet."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:B"
        ).execute(num_retries=5)
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
        ).execute(num_retries=5)
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
        ).execute(num_retries=5)
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

    回傳狀態字串（爬蟲可忽略；sheets_reconcile 用來判斷要不要重試）：
      "ok" 已寫入 / "exists" 已存在 / "failed" 寫入失敗（已記入待補清單並通知）/
      "blocked" 解析異常擋下 / "empty" 沒有可寫的列 / "no_service" 沒有憑證
    """
    service = _get_service()
    if service is None:
        return "no_service"

    _ensure_tab_and_header(service)

    if _already_exists(service, etf_code, data_date):
        log.info(f"Sheets: {etf_code} {data_date} already exists — skipped.")
        _set_pending(etf_code, data_date, None)
        return "exists"

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
        return "empty"

    # 解析異常守門：持股檔數相對前一交易日驟降（來源版面變動的典型徵兆）就不寫入，
    # 避免壞資料污染試算表（快照、歷史、日報全由它衍生）。
    ok, why = holdings_count_check(etf_code, len(rows), data_date)
    if not ok:
        log.error(f"Sheets: {etf_code} {data_date} 已擋下寫入 — {why}")
        blocked = _load_state(BLOCKED_FILE)
        if _key(etf_code, data_date) not in blocked:     # 同一天只警告一次（對帳每輪都會再檢查）
            blocked[_key(etf_code, data_date)] = why
            _save_state(BLOCKED_FILE, blocked)
            _notify(chr(10).join([
                f"⚠️ {etf_code} 疑似解析異常，已擋下寫入試算表",
                f"📅 資料日期：{data_date}",
                why,
                "請檢查該來源版面是否變動（持股檔數突然大幅減少）。",
            ]))
        return "blocked"

    # 送出前把 NaN/Inf 換成空白：NaN 不是合法 JSON，整批會被 API 以 HTTP 400 拒收
    # （00980A 2026-07-30 即因此整天沒寫進去）
    def _clean(v):
        return "" if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))) else v
    rows = [[_clean(v) for v in r] for r in rows]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:L",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}
        ).execute(num_retries=5)
    except Exception as e:
        log.error(f"Sheets append failed for {etf_code} {data_date}: {e}")
        if not _set_pending(etf_code, data_date, e):     # 第一次失敗才通知，之後由對帳自動重試
            _notify(chr(10).join([
                f"⚠️ {etf_code} 寫入試算表失敗",
                f"📅 資料日期：{data_date}",
                f"原因：{str(e)[:200]}",
                "已記入待補清單，下一輪排程會自動重試（sheets_reconcile.py）。",
            ]))
        return "failed"

    log.info(f"Sheets: appended {len(rows)} rows for {etf_code} {data_date} "
             f"(市值={total_market_cap}億, 股數={total_shares}張).")
    if _set_pending(etf_code, data_date, None):
        log.info(f"Sheets: {etf_code} {data_date} 先前寫入失敗，已補寫完成。")
    return "ok"
