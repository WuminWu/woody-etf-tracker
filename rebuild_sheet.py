# -*- coding: utf-8 -*-
"""
rebuild_sheet.py — 用本機資料重建全新 Google 試算表的 holdings 分頁。

背景：原試算表遭刪除且無法救回。本機 snapshots/*.json（2026-04-22~2026-08-06）
與 data_*.json（最新交易日 2026-08-07）資料完整，可完整重建歷史。

用法（先在自己的 Google Drive 建一份空白試算表，分享 Editor 給服務帳戶，取得新 ID）：
    # 新 ID 可用參數或環境變數 NEW_SHEET_ID 提供
    python rebuild_sheet.py 1AbC...XyZ

會做的事：
    1. 確保有 'holdings' 與 'common_actions' 兩個分頁
    2. 清空 holdings 分頁，寫入表頭 + 全部歷史列（A:L）
    3. 依日期、ETF、rank 排序，格式與 sheets_helper.HEADER_ROW 對齊

之後請把新 SHEET_ID 更新到 sheets_helper.py / export_history.py /
export_snapshots.py / record_common_actions.py，再跑一次 export_snapshots.py、
export_history.py、record_common_actions.py 重新產生下游檔案。
"""

import json
import os
import sys
import glob

from sheets_helper import HEADER_ROW, SHEET_TAB, SCOPES

DATA_LATEST_DATE = "2026-08-07"  # data_*.json 補上 snapshot 之後的最後一個交易日


def _load_creds_env():
    """若尚未設定 GOOGLE_SHEETS_CREDENTIALS，從 google_sa.json 載入。"""
    if os.environ.get("GOOGLE_SHEETS_CREDENTIALS"):
        return
    sa = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_sa.json")
    if os.path.exists(sa):
        with open(sa, "r", encoding="utf-8") as f:
            os.environ["GOOGLE_SHEETS_CREDENTIALS"] = f.read()


def _service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"]), scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _row_from_holding(date, etf, h, total_mcap, total_shares):
    """回傳一列 A:L，欄位順序對齊 sheets_helper.HEADER_ROW。"""
    return [
        date,                       # A 日期
        etf,                        # B ETF代號
        h.get("code", ""),          # C 股票代號
        h.get("name", ""),          # D 股票名稱
        h.get("shares", 0),         # E 股數
        h.get("todayWeight", 0),    # F 持股比例(%)
        h.get("yestWeight", 0),     # G 前日比例(%)
        h.get("price", 0),          # H 股價
        h.get("diffShares", 0),     # I 加減碼股數
        h.get("diffAmount", 0),     # J 加減碼金額
        total_mcap,                 # K 基金市值(億)
        total_shares,               # L 基金股數(張)
    ]


def collect_rows():
    """從 snapshots + data_*.json 收集所有歷史列。"""
    rows = []
    seen = set()  # (date, etf) 去重，snapshot 優先

    # --- 1) snapshots/*.json （每個檔含當日全部 ETF）---
    snap_files = sorted(glob.glob("snapshots/????-??-??.json"))
    for sf in snap_files:
        with open(sf, "r", encoding="utf-8") as f:
            snap = json.load(f)
        for etf, blk in snap.items():
            meta = blk.get("meta", {})
            date = meta.get("dataDate") or os.path.basename(sf)[:10]
            key = (date, etf)
            if key in seen:
                continue
            seen.add(key)
            tmc = meta.get("totalMarketCap", "")
            tsh = meta.get("totalShares", "")
            for h in blk.get("holdings", []):
                rows.append(_row_from_holding(date, etf, h, tmc, tsh))

    # --- 2) data_*.json （補 snapshot 之後的最後交易日，如 8/7）---
    for df in sorted(glob.glob("data_0*.json")):
        with open(df, "r", encoding="utf-8") as f:
            d = json.load(f)
        meta = d.get("meta", {})
        date = meta.get("dataDate")
        etf = os.path.basename(df)[len("data_"):-len(".json")]
        if not date:
            continue
        key = (date, etf)
        if key in seen:
            continue
        seen.add(key)
        tmc = meta.get("totalMarketCap", "")
        tsh = meta.get("totalShares", "")
        for h in d.get("holdings", []):
            rows.append(_row_from_holding(date, etf, h, tmc, tsh))

    # 依 日期 → ETF → rank(用 todayWeight 遞減近似原順序) 排序
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def ensure_tabs(service, sheet_id):
    """確保 holdings / common_actions 兩個分頁都存在。"""
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    requests = []
    for tab in (SHEET_TAB, "common_actions"):
        if tab not in existing:
            requests.append({"addSheet": {"properties": {"title": tab}}})
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()
        print(f"已建立分頁: {[r['addSheet']['properties']['title'] for r in requests]}")


def main():
    new_id = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("NEW_SHEET_ID", "")
    new_id = new_id.strip()
    if not new_id:
        print("用法: python rebuild_sheet.py <NEW_SHEET_ID>")
        print("（先在自己的 Google Drive 建空白試算表，分享 Editor 給服務帳戶）")
        sys.exit(1)
    # 允許直接貼整條網址
    if "/spreadsheets/d/" in new_id:
        new_id = new_id.split("/spreadsheets/d/")[1].split("/")[0]

    _load_creds_env()
    if not os.environ.get("GOOGLE_SHEETS_CREDENTIALS"):
        print("找不到憑證：請確認 google_sa.json 存在或設定 GOOGLE_SHEETS_CREDENTIALS")
        sys.exit(1)

    service = _service()
    print(f"目標試算表 ID: {new_id}")

    ensure_tabs(service, new_id)

    rows = collect_rows()
    dates = sorted({r[0] for r in rows})
    etfs = sorted({r[1] for r in rows})
    print(f"收集到 {len(rows)} 列；日期 {dates[0]} ~ {dates[-1]}（{len(dates)} 天）；{len(etfs)} 檔 ETF")

    # 清空 holdings 後寫入表頭 + 全部資料
    service.spreadsheets().values().clear(
        spreadsheetId=new_id, range=f"{SHEET_TAB}!A:L"
    ).execute()

    body = {"values": [HEADER_ROW] + rows}
    result = service.spreadsheets().values().update(
        spreadsheetId=new_id,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        body=body,
    ).execute()
    print(f"已寫入 {result.get('updatedRows', len(rows)+1)} 列到 '{SHEET_TAB}' 分頁。")
    print("完成。接著請更新各程式的 SHEET_ID，再跑 export_snapshots / export_history / record_common_actions。")


if __name__ == "__main__":
    main()
