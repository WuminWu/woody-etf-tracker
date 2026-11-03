"""
tw_prices.py
------------
證交所／櫃買中心官方收盤價（上市＋上櫃，含 ETF），取代逐檔查 yfinance：
一次請求拿到全市場，不會有 NaN、新掛牌代號也查得到（yfinance 掛牌頭幾天常查無資料）。

  closes_for("2026-11-02")  → {"2330": 2330.0, "3081": 2415.0, ...}；當天尚未公布或休市 → {}
  close("2330", "2026-11-02") → 2330.0 或 None

來源（皆可指定日期）：
  上市 https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
       未公布時 stat 不是 "OK"
  上櫃 https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d=ROC/MM/DD&o=json
       ★未公布或休市時會「默默回前一個交易日」的資料，一定要核對回傳的 date
兩邊都拿到才寫入 price_cache/{日期}.json（不進 git），同一天其他爬蟲直接讀快取；
沒拿到的結果也記在本程序記憶體，避免同一支爬蟲每檔持股各打一次 API。
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_cache")
_MEMO = {}

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TPEX_URL = ("https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
            "stk_quote_result.php?l=zh-tw&d={roc}&o=json")


def _get_json(url, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:   # TPEx 檔案約 5MB，偶爾讀到一半斷線（IncompleteRead）
            last = e
    raise last


def _num(s):
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v > 0 else None
    except ValueError:
        return None   # "--"（當日無成交）等


def _table(tables, code_field, close_field):
    for t in tables or []:
        f = t.get("fields") or []
        if code_field in f and close_field in f and t.get("data"):
            ci, pi = f.index(code_field), f.index(close_field)
            return {str(r[ci]).strip(): _num(r[pi]) for r in t["data"]}
    return {}


def _fetch_twse(date_str):
    d = _get_json(TWSE_URL.format(ymd=date_str.replace("-", "")))
    if d.get("stat") != "OK" or d.get("date") != date_str.replace("-", ""):
        return {}
    return _table(d.get("tables"), "證券代號", "收盤價")


def _fetch_tpex(date_str):
    y, m, dd = date_str.split("-")
    d = _get_json(TPEX_URL.format(roc=f"{int(y) - 1911}/{m}/{dd}"))
    if d.get("date") != date_str.replace("-", ""):
        return {}   # 回的是前一個交易日 → 這天還沒有
    return _table(d.get("tables"), "代號", "收盤")


def closes_for(date_str):
    if date_str in _MEMO:
        return _MEMO[date_str]
    path = os.path.join(CACHE_DIR, f"{date_str}.json")
    if os.path.exists(path):
        try:
            _MEMO[date_str] = json.load(open(path, encoding="utf-8"))
            return _MEMO[date_str]
        except Exception:
            pass
    prices = {}
    complete = True
    for name, fn in (("TWSE", _fetch_twse), ("TPEx", _fetch_tpex)):
        try:
            part = fn(date_str)
        except Exception as e:
            log.warning(f"{name} {date_str} 收盤價抓取失敗：{e}")
            part = {}
        if not part:
            complete = False
        prices.update({k: v for k, v in part.items() if v})
    if complete:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False)
        log.info(f"官方收盤價 {date_str}：{len(prices)} 檔（已快取）")
    else:
        log.info(f"官方收盤價 {date_str} 尚未齊全（{len(prices)} 檔），缺的改用 yfinance")
    _MEMO[date_str] = prices
    return prices


def close(code, date_str):
    return closes_for(date_str).get(str(code).strip())
