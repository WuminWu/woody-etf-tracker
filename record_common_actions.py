"""
record_common_actions.py
-------------------------
讀取所有 data_*.json，計算「共同加碼 / 共同減碼」標的（≥2 檔 ETF 同向動作），
寫入 Google Sheets 的 `common_actions` 工作表。

定義：
  加碼 = shares > prevShares  (含「新增」prev=0 的情形)
  減碼 = shares < prevShares  (含「出清」curr=0 的情形)

欄位：
  A: 日期         YYYY-MM-DD
  B: 類型         加碼 / 減碼
  C: 股票代號     e.g. 2330
  D: 股票名稱     e.g. 台積電
  E: ETF數量      共同動作的 ETF 數量
  F: ETF清單      e.g. "00981A,00982A,00985A"

每次執行會先刪除當日已存在的紀錄，再寫入最新結果（idempotent，可重複執行）。
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_ID = "1Wrz6y-DJSTM0oWMa0JRztryEiPwLB8QRU8HJYxHra00"
SHEET_TAB = "common_actions"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER_ROW = ["日期", "類型", "股票代號", "股票名稱", "ETF數量", "ETF清單"]

ETFS = [
    ("00981A", "統一台股增長"),
    ("00400A", "國泰動能高息"),
    ("00403A", "統一升級50"),
    ("00988A", "統一全球創新"),
    ("00980A", "野村智慧優選"),
    ("00985A", "野村台灣50"),
    ("00991A", "復華未來50"),
    ("00992A", "群益科技創新"),
    ("00982A", "群益台灣強棒"),
    ("00987A", "台新台灣優勢成長"),
    ("00993A", "主動安聯台灣"),
    ("00995A", "主動中信台灣卓越"),
    ("00996A", "主動兆豐台灣豐收"),
    ("00405A", "主動富邦台灣龍耀"),
    ("00997A", "主動群益美國增長"),
]


def _get_service():
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json:
        log.warning("GOOGLE_SHEETS_CREDENTIALS not set — skipping Sheets update.")
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.warning(f"Google Sheets auth failed: {e}")
        return None


def _get_sheet_id(service):
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            return s["properties"]["sheetId"]
    return None


def _ensure_tab_and_header(service):
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SHEET_TAB not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]},
        ).execute()
        log.info(f"Created sheet tab: {SHEET_TAB}")
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER_ROW]},
        ).execute()
        return

    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:F1"
    ).execute()
    current = result.get("values", [[]])[0] if result.get("values") else []
    if not current:
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER_ROW]},
        ).execute()


def _delete_existing_rows_for_date(service, target_date):
    """刪除 sheet 中所有日期 == target_date 的列。"""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A:A"
        ).execute()
        rows = result.get("values", [])
    except Exception as e:
        log.warning(f"Failed to read sheet for delete: {e}")
        return 0

    delete_indices = [i for i, r in enumerate(rows) if i > 0 and r and r[0] == target_date]
    if not delete_indices:
        return 0

    sheet_id = _get_sheet_id(service)
    if sheet_id is None:
        return 0

    requests = [
        {"deleteDimension": {"range": {
            "sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": i, "endIndex": i + 1,
        }}}
        for i in sorted(delete_indices, reverse=True)
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID, body={"requests": requests}
    ).execute()
    log.info(f"Deleted {len(delete_indices)} existing rows for {target_date}.")
    return len(delete_indices)


def compute_common_actions():
    """
    回傳 (data_date, add_rows, reduce_rows)
      add_rows / reduce_rows: list of dict {code, name, etfs: [(id, name)]}
    """
    add_map = defaultdict(lambda: {"name": "", "etfs": []})
    reduce_map = defaultdict(lambda: {"name": "", "etfs": []})
    latest_data_date = None

    for etf_id, etf_name in ETFS:
        path = Path(f"data_{etf_id}.json")
        if not path.exists():
            log.info(f"  Skip {etf_id} (no data file)")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  Skip {etf_id} (read failed: {e})")
            continue

        d = data.get("meta", {}).get("dataDate")
        if d and (latest_data_date is None or d > latest_data_date):
            latest_data_date = d

        for h in data.get("holdings", []):
            curr = h.get("shares", 0) or 0
            prev = h.get("prevShares", 0) or 0
            if curr == prev:
                continue
            target = add_map if curr > prev else reduce_map
            entry = target[h["code"]]
            entry["name"] = h.get("name", "")
            entry["etfs"].append((etf_id, etf_name))

    def to_rows(m):
        rows = []
        for code, info in m.items():
            if len(info["etfs"]) < 2:
                continue
            rows.append({"code": code, "name": info["name"], "etfs": info["etfs"]})
        rows.sort(key=lambda r: (-len(r["etfs"]), r["code"]))
        return rows

    return latest_data_date, to_rows(add_map), to_rows(reduce_map)


def main():
    data_date, add_rows, reduce_rows = compute_common_actions()
    if not data_date:
        # fallback: 用今天日期
        data_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    log.info(f"Common-add: {len(add_rows)} stocks; Common-reduce: {len(reduce_rows)} stocks (date={data_date}).")
    for r in add_rows[:5]:
        log.info(f"  [加碼] {r['code']} {r['name']} ×{len(r['etfs'])}")
    for r in reduce_rows[:5]:
        log.info(f"  [減碼] {r['code']} {r['name']} ×{len(r['etfs'])}")

    service = _get_service()
    if service is None:
        log.info("No sheets service available, skipping upload.")
        return

    _ensure_tab_and_header(service)
    _delete_existing_rows_for_date(service, data_date)

    sheet_rows = []
    for r in add_rows:
        etf_ids = ",".join(e[0] for e in r["etfs"])
        sheet_rows.append([data_date, "加碼", r["code"], r["name"], len(r["etfs"]), etf_ids])
    for r in reduce_rows:
        etf_ids = ",".join(e[0] for e in r["etfs"])
        sheet_rows.append([data_date, "減碼", r["code"], r["name"], len(r["etfs"]), etf_ids])

    if not sheet_rows:
        log.info("No common-action rows to upload.")
        return

    try:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A:F",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": sheet_rows},
        ).execute()
        log.info(f"Sheets: appended {len(sheet_rows)} common-action rows for {data_date}.")
    except Exception as e:
        log.warning(f"Sheets append failed: {e}")


if __name__ == "__main__":
    main()
