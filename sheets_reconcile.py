# -*- coding: utf-8 -*-
"""
sheets_reconcile.py — 試算表對帳：寫入失敗的日子自動補寫，並找出 holdings 與試算表的落差。

為什麼需要：試算表 `holdings` 是快照、歷史、週報、連續天數的唯一來源，但爬蟲寫入失敗時
原本只記一行 warning；holdings 檔已存在，之後每輪都直接跳過，那天就永遠缺在試算表裡。
實例：00980A 2026-07-30 寫入時 HTTP 400（payload 含 NaN），holdings 有檔、試算表沒有。

三種用法：
  python sheets_reconcile.py
      排程用（run_update.ps1，在 export_snapshots 之前）。讀一次試算表 A:B，
      各 ETF 目前 data_*.json 的資料日若不在試算表就補寫；先前失敗、但 data 檔已換成
      新日期的，從 git 歷史找回當天爬蟲寫出的原始 data 檔補寫。
  python sheets_reconcile.py --gaps [--days N] [--notify]
      不需憑證：比對 holdings/ 與 snapshots/manifest.json（由試算表匯出），列出缺漏、
      以及標在休市日／週末的資料。--days 只看最近 N 天；--notify 有缺漏就發 Telegram（看門狗用）。
  python sheets_reconcile.py --fill-from-git 00980A 2026-07-30 [--apply]
      手動補某一天：從 git 歷史取回當天爬蟲寫出的 data_00980A.json（價格、加減碼、規模都是
      當時的原值），沒加 --apply 只印不寫。
"""
import glob
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import logging   # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("sheets_reconcile")

from tw_calendar import is_trading_day   # noqa: E402

HOLDINGS_RE = re.compile(r"^(\w+)_holdings_(\d{4}-\d{2}-\d{2})\.json$")


# ---------------------------------------------------------------------------
# 本機資料
# ---------------------------------------------------------------------------

def etf_codes():
    """目前追蹤中的 ETF（有 data_*.json 的）。"""
    return sorted(m.group(1) for m in (re.match(r"^data_(\w+A)\.json$", os.path.basename(p))
                                       for p in glob.glob("data_*.json")) if m)


def load_data_file(code):
    try:
        with open(f"data_{code}.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def data_from_git(code, data_date):
    """從 git 歷史找出 meta.dataDate == data_date 的 data_{code}.json，取最早那一版
    （＝當天爬蟲寫出的原始內容；之後的版本可能被其他腳本改過市值等欄位）。找不到回 None。"""
    path = f"data_{code}.json"
    d = date.fromisoformat(data_date)
    since = (d - timedelta(days=3)).isoformat()
    until = (d + timedelta(days=15)).isoformat()
    try:
        commits = subprocess.run(
            ["git", "log", "--reverse", "--format=%H", f"--since={since}", f"--until={until}", "--", path],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout.split()
    except Exception as e:
        log.warning(f"git log 失敗：{e}")
        return None, None
    for h in commits:
        try:
            raw = subprocess.run(["git", "show", f"{h}:{path}"], capture_output=True,
                                 text=True, encoding="utf-8", check=True).stdout
            doc = json.loads(raw)
        except Exception:
            continue
        if doc.get("meta", {}).get("dataDate") == data_date:
            return doc, h
    return None, None


# ---------------------------------------------------------------------------
# 排程模式：補寫
# ---------------------------------------------------------------------------

def sheet_pairs(service):
    """試算表已有的 {(日期, ETF)}。整張表只讀 A:B 一次。"""
    import sheets_helper as sh
    res = service.spreadsheets().values().get(
        spreadsheetId=sh.SHEET_ID, range=f"{sh.SHEET_TAB}!A:B").execute(num_retries=5)
    return {(r[0], r[1]) for r in res.get("values", [])[1:] if len(r) >= 2}


def run_pipeline():
    import sheets_helper as sh
    service = sh._get_service()
    if service is None:
        log.info("沒有試算表憑證，略過對帳。")
        return 0
    pairs = sheet_pairs(service)
    pending = sh.pending_writes()
    fixed, failed = [], []

    def _append(code, doc, how):
        meta = doc.get("meta", {})
        st = sh.append_holdings_to_sheets(code, meta["dataDate"], doc.get("holdings", []), meta=meta)
        (fixed if st in ("ok", "exists") else failed).append(f"{code} {meta['dataDate']}（{how}：{st}）")

    # 1) 每檔目前的 data 檔：資料日不在試算表就補寫（涵蓋寫入失敗、或爬蟲寫完 data 就中斷）
    for code in etf_codes():
        doc = load_data_file(code)
        dd = (doc or {}).get("meta", {}).get("dataDate")
        if not dd or (dd, code) in pairs:
            if dd and sh._key(code, dd) in pending:
                sh._set_pending(code, dd, None)
            continue
        if sh.is_blocked(code, dd):
            log.info(f"{code} {dd} 先前因解析異常被擋下，對帳不重試。")
            continue
        _append(code, doc, "data 檔")

    # 2) 待補清單裡，data 檔已經換成新日期的 → 從 git 歷史取回原始內容
    for k in sorted(sh.pending_writes()):
        code, dd = k.split("|", 1)
        if (dd, code) in pairs:
            sh._set_pending(code, dd, None)
            continue
        cur = (load_data_file(code) or {}).get("meta", {}).get("dataDate")
        if cur == dd:
            continue                     # 第 1 步已處理（失敗的話仍留在清單上）
        doc, commit = data_from_git(code, dd)
        if doc is None:
            failed.append(f"{code} {dd}（git 歷史找不到當天的 data 檔，請手動處理）")
            continue
        _append(code, doc, f"git {commit[:7]}")

    for x in fixed:
        log.info(f"已補寫：{x}")
    for x in failed:
        log.warning(f"未能補寫：{x}")
    if not fixed and not failed:
        log.info(f"對帳完成：{len(etf_codes())} 檔目前的資料日都已在試算表內。")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# 缺漏檢查（不需憑證）：holdings/ vs snapshots/manifest.json
# ---------------------------------------------------------------------------

def find_gaps(days=None, today=None):
    """回傳 dict：
         missing  [(ETF, 日期)]  holdings 有檔，但試算表（manifest）沒有
         extra    [(ETF, 日期)]  試算表有，但 holdings 沒有檔（多半是日期標錯）
         holiday  [(ETF, 日期, 來源)]  標在週末／休市日的資料
    days 只看最近 N 天；每檔只從它在試算表的第一天開始比（試算表 2026-04-22 才開始累積）。"""
    try:
        manifest = json.load(open(os.path.join("snapshots", "manifest.json"), encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"讀不到 snapshots/manifest.json：{e}")
    in_sheet = {etf: set(ds) for etf, ds in manifest.get("etfs", {}).items()}
    in_hold = {}
    for p in glob.glob(os.path.join("holdings", "*_holdings_*.json")):
        m = HOLDINGS_RE.match(os.path.basename(p))
        if m:
            in_hold.setdefault(m.group(1), set()).add(m.group(2))
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat() if days else ""
    tracked = set(etf_codes())

    def _not_trading(ds):
        return not is_trading_day(date.fromisoformat(ds))

    out = {"missing": [], "extra": [], "holiday": []}
    today_s = today.isoformat()
    for etf in sorted(tracked):
        hs, ss = in_hold.get(etf, set()), in_sheet.get(etf, set())
        if not ss:
            continue                     # 新基金尚未匯出過
        start = max(min(ss), cutoff)
        for ds in sorted(hs - ss):
            if not (start <= ds <= today_s):
                continue
            if _not_trading(ds):
                out["holiday"].append((etf, ds, "holdings 檔"))
            else:
                out["missing"].append((etf, ds))
        for ds in sorted(ss - hs):
            if ds < cutoff:
                continue
            if _not_trading(ds):
                out["holiday"].append((etf, ds, "試算表"))
            else:
                out["extra"].append((etf, ds))
        for ds in sorted(hs & ss):
            if ds >= cutoff and _not_trading(ds):
                out["holiday"].append((etf, ds, "holdings 檔＋試算表"))
    return out


def format_gaps(g):
    lines = []
    if g["missing"]:
        lines.append(f"❗ holdings 有、試算表沒有（{len(g['missing'])} 筆）：")
        lines += [f"  • {e} {d}　補寫：python sheets_reconcile.py --fill-from-git {e} {d} --apply"
                  for e, d in g["missing"]]
    if g["extra"]:
        lines.append(f"❔ 試算表有、holdings 沒有檔（{len(g['extra'])} 筆，多半是日期標錯）：")
        lines += [f"  • {e} {d}" for e, d in g["extra"]]
    if g["holiday"]:
        lines.append(f"📅 標在週末／休市日的資料（{len(g['holiday'])} 筆）：")
        lines += [f"  • {e} {d}（{src}）" for e, d, src in g["holiday"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 手動：從 git 歷史補某一天
# ---------------------------------------------------------------------------

def fill_from_git(code, data_date, apply=False):
    doc, commit = data_from_git(code, data_date)
    if doc is None:
        print(f"git 歷史找不到 data_{code}.json 資料日 {data_date} 的版本")
        return 1
    meta, hs = doc["meta"], doc.get("holdings", [])
    active = [h for h in hs if h.get("todayWeight", 0) > 0]
    print(f"{code} {data_date}：取自 commit {commit[:7]}（lastUpdate {meta.get('lastUpdate')}）")
    print(f"  持股 {len(active)} 檔、規模 {meta.get('totalShares'):,} 張、市值 {meta.get('totalMarketCap')} 億")
    for h in active[:3]:
        print(f"    {h['code']} {h['name']}  股數 {h['shares']:,}  價 {h.get('price')}  "
              f"加減碼 {h.get('diffShares', 0):+,}")
    if not apply:
        print("  [乾跑] 未寫入；確認無誤後加 --apply")
        return 0
    import sheets_helper as sh
    st = sh.append_holdings_to_sheets(code, data_date, hs, meta=meta)
    print(f"  寫入結果：{st}")
    return 0 if st in ("ok", "exists") else 1


def main(argv):
    if "--fill-from-git" in argv:
        i = argv.index("--fill-from-git")
        return fill_from_git(argv[i + 1], argv[i + 2], apply="--apply" in argv)
    if "--gaps" in argv:
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else None
        g = find_gaps(days)
        text = format_gaps(g)
        print(text or f"沒有缺漏（{'最近 %d 天' % days if days else '全部日期'}）")
        if text and "--notify" in argv:
            from dotenv import load_dotenv
            load_dotenv(".env")
            from notify import send_telegram
            send_telegram("🧾 試算表對帳發現缺漏\n" + text)
        return 1 if g["missing"] else 0
    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
