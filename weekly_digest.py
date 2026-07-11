"""
weekly_digest.py
----------------
每週持股變化週報：「本週最後交易日 vs 上週最後交易日」的持股比較，
格式與日報相同（重用 daily_digest.render_digest），台股/海外各發一份 Telegram。

發送時機：
  - 週五：日常 pipeline 末段執行，待當日 snapshot 產生且該組足量 ETF 入列後發送
  - 週六：補發（週五若為颱風假等休市日，run_update.ps1 的週六分支會呼叫本腳本）
  - 其餘日：直接跳過
  marker `last_weekly.txt`（JSON：{"tw": "2026-W28", ...}）防同週重發。

資料來源：snapshots/{date}.json（每檔 ETF 取「該 ISO 週內自己最新的揭露日」，
天然處理海外 T+1 與颱風假）。共識升溫/退潮以「上週 vs 上上週」為基準。

用法：
  python weekly_digest.py                # 排程模式（依上述時機自行判斷）
  python weekly_digest.py --preview      # 產生本週報告並印出，不發送不寫 marker
  python weekly_digest.py --backfill     # 回填歷史每一週到 digests_weekly*.json（不發送）
"""

import glob
import json
import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from daily_digest import (
    render_digest, send_telegram, save_digest,
    TW_ETFS, OVERSEAS_ETFS, GROUPS, SITE_URL,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

MARKER_FILE = "last_weekly.txt"

WEEKLY_GROUPS = {
    "tw": {
        "etfs": TW_ETFS,
        "title": "📊 {rng} 主動 ETF 經理人本週都在買什麼（台股週報）",
        "subhead": "（{u}/{t} 檔納入；比較基準：本週 vs 上週最後交易日）",
        "digests_file": "digests_weekly.json",
        "min_etfs": 10,
        "contributors_from": OVERSEAS_ETFS,   # 海外 ETF 的台股部位一併計入
    },
    "overseas": {
        "etfs": OVERSEAS_ETFS,
        "title": "🌍 {rng} 主動 ETF 經理人本週都在買什麼（海外/混合週報）",
        "subhead": "（{u}/{t} 檔海外/混合 ETF；各檔取該週最新揭露日）",
        "digests_file": "digests_weekly_overseas.json",
        "min_etfs": 1,
        "contributors_from": [],
    },
}


# ---------- snapshot / 週 索引 ----------

def week_id(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def load_snapshot_index():
    """回傳 {date_str: {etf: block}}（lazy：只記路徑，用到才讀）。"""
    return sorted(os.path.basename(f)[:10] for f in glob.glob("snapshots/????-??-??.json"))


_SNAP_CACHE = {}

def read_snap(date_str):
    if date_str not in _SNAP_CACHE:
        try:
            _SNAP_CACHE[date_str] = json.loads(
                open(f"snapshots/{date_str}.json", encoding="utf-8").read())
        except Exception:
            _SNAP_CACHE[date_str] = {}
    return _SNAP_CACHE[date_str]


def dates_by_week(all_dates):
    out = {}
    for ds in all_dates:
        out.setdefault(week_id(date.fromisoformat(ds)), []).append(ds)
    return out


def etf_latest_in(dates, etf):
    """該 ETF 在 dates（升冪）中最後一個有資料的日期。"""
    for ds in reversed(dates):
        if etf in read_snap(ds):
            return ds
    return None


# ---------- 合成週差異 ----------

def build_week_diff(etf, d1, d0):
    """以 snap[d1] vs snap[d0] 合成 render_digest 可吃的 {holdings, meta}。d0 可為 None（新 ETF）。"""
    blk1 = read_snap(d1).get(etf)
    if not blk1:
        return None
    blk0 = read_snap(d0).get(etf) if d0 else None
    map0 = {h["code"]: h for h in blk0.get("holdings", [])} if blk0 else {}

    holdings = []
    seen = set()
    for h1 in blk1.get("holdings", []):
        c = h1["code"]; seen.add(c)
        h0 = map0.get(c, {})
        prev = h0.get("shares", 0)
        ds = h1.get("shares", 0) - prev
        price = h1.get("price", 0) or 0
        holdings.append({
            "code": c, "name": h1.get("name", c),
            "shares": h1.get("shares", 0), "prevShares": prev,
            "price": price,
            "todayWeight": h1.get("todayWeight", 0), "yestWeight": h0.get("todayWeight", 0),
            "diffShares": ds, "diffAmount": round(ds * price, 2),
        })
    for c, h0 in map0.items():          # 上週有、本週清單消失 = 已出清
        if c in seen or h0.get("shares", 0) <= 0:
            continue
        price = h0.get("price", 0) or 0
        holdings.append({
            "code": c, "name": h0.get("name", c),
            "shares": 0, "prevShares": h0["shares"],
            "price": price, "todayWeight": 0, "yestWeight": h0.get("todayWeight", 0),
            "diffShares": -h0["shares"], "diffAmount": round(-h0["shares"] * price, 2),
        })

    m1, m0 = blk1.get("meta", {}), (blk0.get("meta", {}) if blk0 else {})
    meta = {
        "dataDate": d1,
        "totalMarketCap": m1.get("totalMarketCap", 0),
        "prevTotalMarketCap": m0.get("totalMarketCap", 0),
        "totalShares": m1.get("totalShares", 0),
        "prevTotalShares": m0.get("totalShares", 0),
    }
    return {"holdings": holdings, "meta": meta}


def build_prev_week_snap(etfs, dates_prev, dates_prev2):
    """共識升溫/退潮基準：上週(vs 上上週)的各 ETF 週加碼 → 合成 snapshot 形狀。"""
    out = {}
    for etf, _n in etfs:
        d0 = etf_latest_in(dates_prev, etf)
        dm = etf_latest_in(dates_prev2, etf) if dates_prev2 else None
        if not d0:
            continue
        syn = build_week_diff(etf, d0, dm)
        if syn:
            out[etf] = {"holdings": syn["holdings"]}
    return out or None


def tw_only(syn):
    """海外 ETF 的合成資料只留台股部位（代號無市場後綴）。"""
    if not syn:
        return None
    hs = [h for h in syn["holdings"] if " " not in str(h["code"])]
    return {"holdings": hs, "meta": syn["meta"]} if hs else None


# ---------- 產生單一群組週報 ----------

def build_weekly(group, cur_dates, prev_dates, prev2_dates):
    g = WEEKLY_GROUPS[group]
    etf_data, updated = {}, []
    report_date = None
    for etf, _n in g["etfs"]:
        d1 = etf_latest_in(cur_dates, etf)
        if not d1:
            continue
        d0 = etf_latest_in(prev_dates, etf)
        syn = build_week_diff(etf, d1, d0)
        if not syn:
            continue
        etf_data[etf] = syn
        updated.append(etf)
        report_date = max(report_date or d1, d1)

    contributors = []
    for etf, _n in g["contributors_from"]:
        d1 = etf_latest_in(cur_dates, etf)
        if not d1:
            continue
        d0 = etf_latest_in(prev_dates, etf)
        syn = tw_only(build_week_diff(etf, d1, d0))
        if syn:
            etf_data[etf] = syn
            contributors.append(etf)

    if not updated:
        return None, None, 0

    prev_snap = build_prev_week_snap(g["etfs"], prev_dates, prev2_dates)
    rng = f"{cur_dates[0][5:].replace('-', '/')}~{report_date[5:].replace('-', '/')}"
    title = g["title"].format(rng=rng)
    msg, cnt = render_digest(report_date, etf_data, updated, prev_snap,
                             total_tracked=len(g["etfs"]),
                             title_tmpl=title, subhead_tmpl=g["subhead"],
                             contributors=contributors)
    if msg:
        msg = _weeklyize(msg)
    return report_date, msg, cnt


# 把日報措辭轉為週報語意（render_digest 為日報用語，此處統一轉換）
_WORD_MAP = [
    ("今日總結", "本週總結"),
    ("今天淨買超最多", "本週淨買超最多"),
    ("今天淨賣超最多", "本週淨賣超最多"),
    ("今日觀察", "本週觀察"),
    ("佔今日買超", "佔本週買超"),
    ("今日全部買超金額", "本週全部買超金額"),
    ("前一日多家共識買", "上週多家共識買"),
    ("今日無人續加碼", "本週無人續加碼"),
    ("今日無人續買", "本週無人續買"),
    ("與前一交易日比較", "與上週比較"),
]


def _weeklyize(msg):
    for a, b in _WORD_MAP:
        msg = msg.replace(a, b)
    return msg


# ---------- marker ----------

def load_marker():
    if os.path.exists(MARKER_FILE):
        try:
            return json.loads(open(MARKER_FILE, encoding="utf-8").read())
        except Exception:
            pass
    return {}


def save_marker(m):
    with open(MARKER_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)


# ---------- 模式 ----------

def _week_buckets(ref_day):
    all_dates = load_snapshot_index()
    buckets = dates_by_week(all_dates)
    cur = week_id(ref_day)
    weeks = sorted(buckets.keys())
    def prev_of(w, n=1):
        i = weeks.index(w) if w in weeks else None
        if i is None or i - n < 0:
            return []
        return buckets[weeks[i - n]]
    cur_dates = [d for d in buckets.get(cur, []) if d <= ref_day.isoformat()]
    return cur, cur_dates, prev_of(cur, 1), prev_of(cur, 2)


def run_scheduled():
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    wd = today.weekday()   # 0=Mon
    if wd not in (4, 5):
        log.info("非週五/週六，週報不執行。")
        return
    cur, cur_dates, prev_dates, prev2_dates = _week_buckets(today)
    if not cur_dates or not prev_dates:
        log.info("本週或上週無 snapshot，週報跳過。")
        return
    # 週五：需當日 snapshot 已產生（pipeline 末段執行時通常已就緒）
    if wd == 4 and cur_dates[-1] != today.isoformat():
        log.info("週五但今日 snapshot 尚未產生，待下一輪。")
        return

    marker = load_marker()
    for group, g in WEEKLY_GROUPS.items():
        if marker.get(group) == cur:
            log.info(f"[{group}] 本週（{cur}）週報已發送，跳過。")
            continue
        report_date, msg, cnt = build_weekly(group, cur_dates, prev_dates, prev2_dates)
        if not msg or cnt < g["min_etfs"]:
            log.info(f"[{group}] 資料不足（{cnt} 檔 < {g['min_etfs']}），跳過。")
            continue
        save_digest(report_date, msg, path=g["digests_file"])
        if send_telegram(msg + "\n\n" + SITE_URL):
            marker[group] = cur
            save_marker(marker)
            log.info(f"[{group}] 週報已發送（{cur}，{cnt} 檔）。")
        else:
            log.warning(f"[{group}] 週報發送失敗，marker 未寫（下輪重試）。")


def run_preview():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    cur, cur_dates, prev_dates, prev2_dates = _week_buckets(today)
    for group in WEEKLY_GROUPS:
        report_date, msg, cnt = build_weekly(group, cur_dates, prev_dates, prev2_dates)
        out = f"_weekly_{group}.txt"
        open(out, "w", encoding="utf-8").write(msg or "(無資料)")
        log.info(f"[{group}] preview → {out}（{cnt} 檔，報告日 {report_date}）")


def run_backfill():
    all_dates = load_snapshot_index()
    buckets = dates_by_week(all_dates)
    weeks = sorted(buckets.keys())
    for i, w in enumerate(weeks):
        if i == 0:
            continue   # 第一週無上週基準
        cur_dates = buckets[w]
        prev_dates = buckets[weeks[i - 1]]
        prev2_dates = buckets[weeks[i - 2]] if i >= 2 else []
        for group, g in WEEKLY_GROUPS.items():
            report_date, msg, cnt = build_weekly(group, cur_dates, prev_dates, prev2_dates)
            if msg and cnt >= 1:
                save_digest(report_date, msg, path=g["digests_file"])
        print(f"{w}: 回填完成（{cur_dates[0]} ~ {cur_dates[-1]}）")


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        run_backfill()
    elif "--preview" in sys.argv:
        run_preview()
    else:
        run_scheduled()
