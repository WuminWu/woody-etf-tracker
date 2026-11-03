# -*- coding: utf-8 -*-
"""
big_move_alert.py
-----------------
「短時間大量建倉／砍倉」特別警示：另發一則 🚨 Telegram，不埋在日報裡。

日報的「🆕 新建倉／🗑️ 被清倉」只看單日、以金額（≥1000萬）為門檻：
經理人分 2~3 天建好的倉位每天看都不大而漏抓；大規模 ETF 小動作也會上榜、
小規模 ETF 重押卻上不了榜。這裡改看「權重」在 WINDOW 個交易日內的變化：

  🚀 快速建倉：區間起點權重 < LOW_WT，區間終點 ≥ HIGH_WT，且當日權重仍在升
  🔪 快速砍倉：區間起點權重 ≥ HIGH_WT，終點 ≤ LOW_WT
              或股數砍掉 ≥ CUT_SHARES_PCT 且權重同步降到起點的 40% 以下
              （權重條件排除減資/反分割這類股數變但部位沒變的情況），且當日權重在降
  👥 集體動作：WINDOW 內 ≥ GROUP_MIN 家 ETF 對同一檔同向觸發，合併成一則

區間起點權重＝區間第一天的 yestWeight，所以 WINDOW=3 涵蓋 3 個交易日的異動。
資料來源：snapshots/*.json（export_snapshots.py 產出，本腳本排在它之後、日報之前）。

重複通知防護（alert_state.json，進 git，本機與 GitHub Actions 共用）：
  同一 (ETF, 個股, 方向) 在 SUPPRESS_DAYS 天內只發一次；
  建倉若之後權重再比上次通知時高 ESCALATE_PP 個百分點，以「持續加碼」再發一次。
  每天排程會跑好幾輪，各 ETF 資料陸續進來，有新事件才發，沒有就不發。

用法：
  python big_move_alert.py            # 排程用：偵測 + 發送 + 記錄
  python big_move_alert.py --dry-run  # 只印出訊息，不發送、不寫 state
  python big_move_alert.py --seed     # 把目前偵測到的事件全部標為已通知（首次上線用，不發送）
"""

import argparse
import glob
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

from market_utils import is_stock_code
from notify import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

WINDOW = 3              # 交易日（snapshot 日期數）
LOW_WT = 0.5            # %
HIGH_WT = 2.0           # %
CUT_SHARES_PCT = 70     # 股數砍掉幾 % 以上算砍倉
GROUP_MIN = 3           # 幾家 ETF 同向觸發算集體動作
EVAL_DATES = 3          # 檢查最近幾個 snapshot 日（海外 ETF T+1，資料會晚一天補進前一個 snapshot）
SUPPRESS_DAYS = 7       # 同一事件幾天內不重發
ESCALATE_PP = 2.0       # 建倉後權重再升幾個百分點就再通知一次
STATE_FILE = "alert_state.json"
SITE_URL = "https://wuminwu.github.io/woody-etf-tracker/"
PROVENANCE = "報告來源: 854-Woody (狼群專用未經同意請勿轉傳，若數據有誤請通知我)"


def load_days(n):
    """最近 n 個 snapshot：[(date, snap), ...]，升冪。"""
    files = sorted(glob.glob("snapshots/????-??-??.json"))[-n:]
    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                out.append((os.path.basename(f)[:10], json.load(fh)))
        except Exception as e:
            log.warning(f"讀取 {f} 失敗：{e}")
    return out


def _is_first_day(holdings):
    """>80% 持股 prevShares=0＝剛納入追蹤的 ETF，整份持股都會像新建倉，略過。"""
    active = [h for h in holdings if h.get("shares", 0) > 0]
    return bool(active) and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8


def _index(days):
    """→ ({(etf, code): {date: row}}, {etf: set(有資料的日期)}, {code: name}, {(etf, date): 規模(億)})"""
    rows = defaultdict(dict)
    has_data = defaultdict(set)
    names = {}
    mcap = {}
    n_rows = {}
    for d, snap in days:
        for etf, blk in snap.items():
            hs = blk.get("holdings", [])
            if not hs or _is_first_day(hs):
                continue
            has_data[etf].add(d)
            mcap[(etf, d)] = (blk.get("meta") or {}).get("totalMarketCap") or 0
            n_rows[(etf, d)] = len(hs)
            for h in hs:
                c = h.get("code", "")
                if h.get("isFutures") or not is_stock_code(c):
                    continue
                names[c] = h.get("name") or c
                rows[(etf, c)][d] = h
    return rows, has_data, names, mcap, n_rows


def detect(days, eval_dates=EVAL_DATES):
    """
    回傳事件清單（每個 (ETF, 個股, 方向) 取最近一個觸發日），每筆：
    {date, etf, code, name, kind('build'/'cut'), w0, w1, cum(元), cum_est(bool), cleared(bool)}
    days 需至少涵蓋 eval_dates + WINDOW - 1 天才能完整判斷最早的檢查日。
    """
    rows, has_data, names, mcap, n_rows = _index(days)
    dates = [d for d, _ in days]
    found = {}
    for i in range(max(0, len(dates) - eval_dates), len(dates)):
        d = dates[i]
        win = dates[max(0, i - WINDOW + 1):i + 1]
        for (etf, c), hist in rows.items():
            if d not in has_data[etf]:
                continue
            first = next((x for x in win if x in hist), None)
            if first is None:
                continue
            h0 = hist[first]
            w0 = h0.get("yestWeight", 0) or 0
            sh0 = h0.get("prevShares", 0) or 0
            h1 = hist.get(d)
            cum = sum((hist[x].get("diffAmount", 0) or 0) for x in win if x in hist)
            cum_est = False
            if h1 is None:
                # 該 ETF 當日有資料但沒有這檔＝已全數出清（部分來源出清後不列 0 股那一列）。
                # 只在「前一個有資料的日子還持有」時算；更早就消失的已在當時判斷過。
                last_seen = max(x for x in hist if x <= d)
                prev_day = max((x for x in has_data[etf] if x < d), default=None)
                if last_seen != prev_day or not hist[last_seen].get("shares", 0):
                    continue
                # 持股列數比前一日少三成以上＝多半是來源只抓到部分清單，不當成出清
                if n_rows[(etf, d)] < n_rows[(etf, prev_day)] * 0.7:
                    continue
                w1, wy, sh1 = 0.0, (hist[last_seen].get("todayWeight", 0) or 0), 0
                # 沒有出清那一列就沒有 diffAmount：以「前一日權重 × 前一日規模」估算賣出金額
                cum -= wy / 100 * mcap.get((etf, last_seen), 0) * 1e8
                cum_est = True
            else:
                w1 = h1.get("todayWeight", 0) or 0
                wy = h1.get("yestWeight", 0) or 0
                sh1 = h1.get("shares", 0) or 0

            kind = None
            if w0 < LOW_WT and w1 >= HIGH_WT and w1 > wy:
                kind = "build"
            elif w0 >= HIGH_WT and w1 < wy and (
                    w1 <= LOW_WT
                    or (sh0 and (sh0 - sh1) / sh0 * 100 >= CUT_SHARES_PCT and w1 <= w0 * 0.4)):
                kind = "cut"
            if kind:
                found[(etf, c, kind)] = {
                    "date": d, "etf": etf, "code": c, "name": names.get(c, c), "kind": kind,
                    "w0": round(w0, 2), "w1": round(w1, 2), "cum": cum, "cum_est": cum_est,
                    "cleared": sh1 == 0,
                }
    return sorted(found.values(), key=lambda e: (e["date"], e["kind"], -abs(e["cum"])))


def _key(e):
    return f"{e['etf']}|{e['code']}|{e['kind']}"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"讀取 {STATE_FILE} 失敗（{e}），視為空")
        return {}


def save_state(state, today):
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    state = {k: v for k, v in state.items() if v.get("date", "") >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def _days_between(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def filter_new(events, state):
    """回傳 [(event, escalated)]：沒通知過、或建倉後權重明顯再升的事件。"""
    out = []
    for e in events:
        prev = state.get(_key(e))
        if prev and _days_between(prev["date"], e["date"]) <= SUPPRESS_DAYS:
            if e["kind"] == "build" and e["w1"] >= prev["w1"] + ESCALATE_PP:
                out.append((e, True))
            continue
        out.append((e, False))
    return out


def _md(date_str):
    m, d = date_str[5:].split("-")
    return f"{int(m)}/{int(d)}"


def _yi(amount):
    v = amount / 1e8
    return f"{'+' if v >= 0 else ''}{v:.1f}億"


def _line(e, escalated=False, ref=None):
    icon = "🚀" if e["kind"] == "build" else "🔪"
    tag = "（持續加碼）" if escalated else ""
    when = f"[{_md(e['date'])}] " if e["date"] != ref else ""   # 海外 T+1 等較早資料日另標日期
    tail = "，全數出清" if e["kind"] == "cut" and e["cleared"] else ""
    amt = ("約 " if e.get("cum_est") else "累計 ") + _yi(e["cum"])
    return (f"{icon} {when}{e['etf']} {e['name']} {e['code']}{tag}：{WINDOW} 日內權重 "
            f"{e['w0']:.2f}% → {e['w1']:.2f}%（{amt}{tail}）")


def render(new, all_events, state):
    """
    new: filter_new 的結果；all_events: 本輪全部偵測到的事件（含已通知過的），
    用來判斷集體動作——之前已發過個別通知的 ETF 也算進家數。
    """
    if not new:
        return None
    new_keys = {_key(e) for e, _ in new}
    by_stock = defaultdict(list)
    for e in all_events:
        by_stock[(e["code"], e["kind"])].append(e)

    group_lines, grouped_keys = [], set()
    for (code, kind), evs in by_stock.items():
        if len(evs) < GROUP_MIN or not any(_key(e) in new_keys for e in evs):
            continue
        gkey = f"GROUP|{code}|{kind}"
        prev = state.get(gkey)
        n_prev = prev.get("n", 0) if prev else 0
        if prev and _days_between(prev["date"], max(e["date"] for e in evs)) <= SUPPRESS_DAYS \
                and len(evs) <= n_prev:
            continue
        evs = sorted(evs, key=lambda e: -abs(e["cum"]))
        verb = "快速建倉" if kind == "build" else "快速砍倉"
        etfs = "、".join(f"{e['etf']}({e['w0']:.1f}→{e['w1']:.1f}%)" for e in evs)
        total = sum(e["cum"] for e in evs)
        group_lines.append((gkey, len(evs), max(e["date"] for e in evs),
                            f"👥 {evs[0]['name']} {code}：{len(evs)} 家同時{verb}，合計 {_yi(total)}\n　{etfs}"))
        grouped_keys.update(_key(e) for e in evs)

    ref = max(e["date"] for e, _ in new)
    builds = [_line(e, esc, ref) for e, esc in new if e["kind"] == "build" and _key(e) not in grouped_keys]
    cuts = [_line(e, esc, ref) for e, esc in new if e["kind"] == "cut" and _key(e) not in grouped_keys]

    lines = [PROVENANCE, "", f"🚨 {_md(ref)} 主動 ETF 快速建倉／砍倉警示",
             f"（{WINDOW} 個交易日內權重 <{LOW_WT:g}% → ≥{HIGH_WT:g}%，或 ≥{HIGH_WT:g}% → ≤{LOW_WT:g}%）", ""]
    if group_lines:
        lines += [g[3] for g in group_lines] + [""]
    if builds:
        lines += builds + [""]
    if cuts:
        lines += cuts + [""]
    lines.append(SITE_URL)
    return "\n".join(lines), group_lines


def mark_sent(state, new, group_lines):
    for e, _ in new:
        state[_key(e)] = {"date": e["date"], "w1": e["w1"]}
    for gkey, n, d, _txt in group_lines:
        state[gkey] = {"date": d, "n": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只印出，不發送、不寫 state")
    ap.add_argument("--seed", action="store_true", help="把目前事件標為已通知，不發送")
    args = ap.parse_args()

    days = load_days(EVAL_DATES + WINDOW - 1)
    if not days:
        log.info("沒有 snapshot，略過")
        return
    events = detect(days)
    state = load_state()
    new = filter_new(events, state)
    log.info(f"偵測到 {len(events)} 筆事件（最近 {EVAL_DATES} 個 snapshot 日），其中 {len(new)} 筆未通知")
    res = render(new, events, state)
    if not res:
        return
    message, group_lines = res

    if args.dry_run:
        print(message)
        return
    if args.seed:
        mark_sent(state, new, group_lines)
        save_state(state, days[-1][0])
        log.info(f"已將 {len(new)} 筆事件標為已通知（未發送）")
        return
    if send_telegram(message):
        mark_sent(state, new, group_lines)
        save_state(state, days[-1][0])
        log.info(f"已發送 {len(new)} 筆快速建倉／砍倉警示")
    else:
        log.warning("發送失敗，state 未更新（下一輪重試）")


if __name__ == "__main__":
    main()
