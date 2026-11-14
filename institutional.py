# -*- coding: utf-8 -*-
"""
institutional.py — 每日三大法人買賣超（現貨：總金額＋個股）與期貨留倉口數。

輸出（皆進 git，網頁「三大法人買賣超」分頁讀取）：
  institutional/{YYYY-MM-DD}.json  單日完整資料
      summary   上市／上櫃 × 外資／投信／自營商／合計 的買進、賣出、買賣超金額（元）
      stocks    個股買賣超股數（上市＋上櫃），rows = [代號, 名稱, 市場(0上市/1上櫃), 收盤價, 外資, 投信, 自營商]
                個股「金額」官方不公布，前端以 股數 × 收盤價 估算
      futures   期交所各期貨契約 × 身份別：交易口數淨額、多/空方未平倉口數、未平倉淨額口數與契約金額（千元）
  institutional/index.json         每日摘要（趨勢表用），由所有單日檔重建、最新在前

來源（皆可指定日期）：
  上市總額  https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json
  上市個股  https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json
  上櫃總額  https://www.tpex.org.tw/web/stock/3insti/3insti_summary/3itrdsum_result.php?l=zh-tw&t=D&p=1&d=ROC/MM/DD&o=json
  上櫃個股  https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d=ROC/MM/DD&o=json
  期貨      https://www.taifex.com.tw/cht/3/futContractsDateDown?queryStartDate=YYYY/MM/DD&queryEndDate=YYYY/MM/DD&commodityId=
  收盤價    tw_prices.closes_for()（同一份快取，其他爬蟲也用）
★ 櫃買中心未公布時會「默默回前一個交易日」，一定要核對回傳日期。

各部分分開抓：還沒公布的部分（期交所約 15:00、證交所個股約 16:30）留給下一輪（run_update.ps1 每 30 分鐘）補，
已抓到的不重抓。預設檢查最近 5 個交易日，電腦沒開的日子會自動補上。

用法：
    python institutional.py                 # 最近 5 個交易日缺的補齊
    python institutional.py --date 2026-11-13
    python institutional.py --backfill 30   # 回補最近 30 個交易日
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import tw_calendar
import tw_prices

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "institutional")
PARTS = ("twse_sum", "tpex_sum", "twse_stk", "tpex_stk", "futures", "closes")
PAUSE = 2.5          # 證交所連續請求太快會被擋，每次請求間隔
UA = {"User-Agent": "Mozilla/5.0"}

# 期貨「大台約當」：小台 1/4 口、微台 1/20 口
TX_EQUIV = {"臺股期貨": 1.0, "小型臺指期貨": 0.25, "微型臺指期貨": 0.05}
PARTY_KEY = {"外資及陸資": "foreign", "投信": "trust", "自營商": "dealer"}


def say(msg):
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.buffer.write((msg + "\n").encode(enc, errors="replace"))
    sys.stdout.buffer.flush()


_last_req = [0.0]


def _get(url, tries=2):
    last = None
    for i in range(tries):
        wait = PAUSE - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                _last_req[0] = time.time()
                return r.read()
        except Exception as e:
            _last_req[0] = time.time()
            last = e
            time.sleep(3 * (i + 1))
    raise last


def _get_json(url):
    return json.loads(_get(url))


def _int(s):
    try:
        return int(float(str(s).replace(",", "").strip()))
    except ValueError:
        return 0


def _roc(d):
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


# ── 上市／上櫃 總金額 ─────────────────────────────────────────
def _party_row(buy, sell, net):
    return {"buy": _int(buy), "sell": _int(sell), "net": _int(net)}


def fetch_twse_sum(d):
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={d:%Y%m%d}&response=json")
    if j.get("stat") != "OK" or j.get("date") != f"{d:%Y%m%d}" or not j.get("data"):
        return None
    rows = {r[0].strip(): r for r in j["data"]}

    def pick(name):
        r = rows.get(name)
        return _party_row(*r[1:4]) if r else {"buy": 0, "sell": 0, "net": 0}

    self_, hedge = pick("自營商(自行買賣)"), pick("自營商(避險)")
    out = {
        "foreign": pick("外資及陸資(不含外資自營商)"),
        "trust": pick("投信"),
        "dealer": {k: self_[k] + hedge[k] for k in ("buy", "sell", "net")},
        "dealer_self": self_,
        "dealer_hedge": hedge,
        "foreign_dealer": pick("外資自營商"),
        "total": pick("合計"),
    }
    return out


def fetch_tpex_sum(d):
    j = _get_json("https://www.tpex.org.tw/web/stock/3insti/3insti_summary/3itrdsum_result.php"
                  f"?l=zh-tw&t=D&p=1&d={_roc(d)}&o=json")
    t = (j.get("tables") or [{}])[0]
    if t.get("date") != _roc(d) or not t.get("data"):
        return None
    rows = {r[0].replace("　", "").strip(): r for r in t["data"]}

    def pick(name):
        r = rows.get(name)
        return _party_row(*r[1:4]) if r else {"buy": 0, "sell": 0, "net": 0}

    return {
        "foreign": pick("外資及陸資(不含自營商)"),
        "trust": pick("投信"),
        "dealer": pick("自營商合計"),
        "dealer_self": pick("自營商(自行買賣)"),
        "dealer_hedge": pick("自營商(避險)"),
        "foreign_dealer": pick("外資自營商"),
        "total": pick("三大法人合計*"),
    }


# ── 個股（股數）─────────────────────────────────────────────
def fetch_twse_stk(d):
    j = _get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json")
    if j.get("stat") != "OK" or j.get("date") != f"{d:%Y%m%d}" or not j.get("data"):
        return None
    f = j["fields"]
    ci, ni = f.index("證券代號"), f.index("證券名稱")
    fi = f.index("外陸資買賣超股數(不含外資自營商)")
    ti = f.index("投信買賣超股數")
    di = f.index("自營商買賣超股數")
    return [[r[ci].strip(), r[ni].strip(), 0, _int(r[fi]), _int(r[ti]), _int(r[di])] for r in j["data"]]


def fetch_tpex_stk(d):
    j = _get_json("https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
                  f"?l=zh-tw&se=EW&t=D&d={_roc(d)}&o=json")
    t = (j.get("tables") or [{}])[0]
    if j.get("date") != f"{d:%Y%m%d}" or t.get("date") != _roc(d) or not t.get("data"):
        return None
    # 欄位：代號,名稱, 外資(不含外資自營商)買/賣/超[2-4], 外資自營商[5-7], 外資合計[8-10],
    #       投信[11-13], 自營商自行買賣[14-16], 自營商避險[17-19], 自營商合計[20-22], 三大法人合計[23]
    if len(t.get("fields") or []) < 24:
        raise ValueError(f"櫃買個股欄位數異常：{t.get('fields')}")
    return [[r[0].strip(), r[1].strip(), 1, _int(r[4]), _int(r[13]), _int(r[22])] for r in t["data"]]


# ── 期貨 ──────────────────────────────────────────────────
def fetch_futures(d):
    ds = urllib.parse.quote(f"{d:%Y/%m/%d}")
    raw = _get("https://www.taifex.com.tw/cht/3/futContractsDateDown"
               f"?queryStartDate={ds}&queryEndDate={ds}&commodityId=")
    text = raw.decode("big5", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "日期" not in rows[0][0]:
        return None   # 查無資料時回 HTML 錯誤頁
    out = []
    for r in rows[1:]:
        if len(r) < 15 or r[0].strip() != f"{d:%Y/%m/%d}":
            continue
        party = PARTY_KEY.get(r[2].strip())
        if not party:
            continue
        out.append({
            "name": r[1].strip(), "party": party,
            "trade_net": _int(r[7]), "trade_net_amt": _int(r[8]),
            "oi_long": _int(r[9]), "oi_short": _int(r[11]),
            "oi_net": _int(r[13]), "oi_net_amt": _int(r[14]),
        })
    return out or None


# ── 組裝 ──────────────────────────────────────────────────
def _path(d):
    return os.path.join(OUT_DIR, f"{d:%Y-%m-%d}.json")


def load_day(d):
    p = _path(d)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def update_day(d):
    """抓 d 這天缺的部分；回傳 (是否有新資料, 是否齊全)。"""
    ds = f"{d:%Y-%m-%d}"
    raw_path = os.path.join(OUT_DIR, "_raw", f"{ds}.json")   # 個股原始股數暫存（等收盤價齊全再合併）
    day = load_day(d) or {"date": ds, "summary": {}, "stocks": None, "futures": None, "parts": []}
    have = set(day.get("parts") or [])
    if have.issuperset(PARTS):
        return False, True

    raw = {}
    if os.path.exists(raw_path):
        try:
            raw = json.load(open(raw_path, encoding="utf-8"))
        except Exception:
            raw = {}

    changed = False
    jobs = [
        ("twse_sum", lambda: fetch_twse_sum(d)),
        ("tpex_sum", lambda: fetch_tpex_sum(d)),
        ("twse_stk", lambda: fetch_twse_stk(d)),
        ("tpex_stk", lambda: fetch_tpex_stk(d)),
        ("futures", lambda: fetch_futures(d)),
    ]
    for part, fn in jobs:
        if part in have or (part in ("twse_stk", "tpex_stk") and part in raw):
            continue
        try:
            v = fn()
        except Exception as e:
            say(f"  {ds} {part} 抓取失敗：{e}")
            v = None
        if v is None:
            say(f"  {ds} {part} 尚未公布")
            continue
        changed = True
        if part == "twse_sum":
            day["summary"]["twse"] = v
        elif part == "tpex_sum":
            day["summary"]["tpex"] = v
        elif part == "futures":
            day["futures"] = v
        else:
            raw[part] = v
        if part not in ("twse_stk", "tpex_stk"):
            have.add(part)

    # 個股：上市、上櫃都到齊且收盤價齊全才合併寫入
    if "twse_stk" in raw and "tpex_stk" in raw and "closes" not in have:
        closes = tw_prices.closes_for(ds)
        complete = os.path.exists(os.path.join(tw_prices.CACHE_DIR, f"{ds}.json"))
        if complete:
            rows = []
            for code, name, mkt, f_, t_, d_ in raw["twse_stk"] + raw["tpex_stk"]:
                if f_ == 0 and t_ == 0 and d_ == 0:
                    continue
                c = closes.get(code)
                if not c:
                    continue   # 無收盤價（當日無成交等），無法估金額
                rows.append([code, name, mkt, c, f_, t_, d_])
            day["stocks"] = {"fields": ["code", "name", "mkt", "close", "foreign", "trust", "dealer"],
                             "rows": rows}
            have.update(("twse_stk", "tpex_stk", "closes"))
            changed = True
        else:
            say(f"  {ds} 收盤價尚未齊全，個股金額下一輪再算")

    if not changed:
        return False, have.issuperset(PARTS)

    os.makedirs(OUT_DIR, exist_ok=True)
    day["parts"] = sorted(have)
    day["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(_path(d), "w", encoding="utf-8") as f:
        json.dump(day, f, ensure_ascii=False, separators=(",", ":"))
    done = have.issuperset(PARTS)
    if done:
        if os.path.exists(raw_path):
            os.remove(raw_path)
    elif raw:
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
    return True, done


def _fut_summary(futures):
    """每身份別：臺股期貨淨留倉、小台、微台、大台約當、全部期貨淨留倉契約金額（千元）。"""
    out = {}
    for p in PARTY_KEY.values():
        rows = [r for r in futures or [] if r["party"] == p]
        by = {r["name"]: r for r in rows}
        out[p] = {
            "tx": by.get("臺股期貨", {}).get("oi_net", 0),
            "mtx": by.get("小型臺指期貨", {}).get("oi_net", 0),
            "tmf": by.get("微型臺指期貨", {}).get("oi_net", 0),
            "tx_equiv": round(sum(by.get(n, {}).get("oi_net", 0) * w for n, w in TX_EQUIV.items()), 2),
            "all_amt": sum(r["oi_net_amt"] for r in rows),
        }
    return out


def rebuild_index():
    if not os.path.isdir(OUT_DIR):
        return 0
    items = []
    for fn in sorted(os.listdir(OUT_DIR), reverse=True):
        if len(fn) != 15 or not fn.endswith(".json") or fn == "index.json":
            continue
        try:
            day = json.load(open(os.path.join(OUT_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        s = day.get("summary") or {}
        spot = {}
        for p in ("foreign", "trust", "dealer", "total"):
            spot[p] = sum((s.get(m) or {}).get(p, {}).get("net", 0) for m in ("twse", "tpex"))
        items.append({
            "date": day["date"],
            "complete": set(day.get("parts") or []).issuperset(PARTS),
            "spot": spot if s else None,
            "futures": _fut_summary(day["futures"]) if day.get("futures") else None,
        })
    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
    return len(items)


def recent_trading_days(n, today=None):
    d = today or date.today()
    if d == date.today() and datetime.now().hour < 14:
        d -= timedelta(days=1)     # 收盤前今天不會有資料
    days = []
    while len(days) < n:
        if tw_calendar.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="只處理這一天 YYYY-MM-DD")
    ap.add_argument("--backfill", type=int, default=5, help="檢查最近 N 個交易日（預設 5）")
    a = ap.parse_args()

    days = [datetime.strptime(a.date, "%Y-%m-%d").date()] if a.date else recent_trading_days(a.backfill)
    pending = []
    for d in sorted(days):
        changed, done = update_day(d)
        state = "齊全" if done else "未齊全"
        if changed:
            say(f"{d} 已更新（{state}）")
        if not done:
            pending.append(str(d))
    n = rebuild_index()
    say(f"index.json：{n} 天" + (f"；未齊全：{', '.join(pending)}" if pending else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
