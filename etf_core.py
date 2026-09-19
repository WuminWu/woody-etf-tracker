# -*- coding: utf-8 -*-
"""
etf_core.py — 19 支 check_and_update_XXXXXA.py 的共用核心。

## 為什麼有這個檔案
原本每支基金一個爬蟲，各自帶著自己的 generate_data_json / get_price /
get_previous_holdings / fmt_zhang…。這些函式 95% 相同，差異幾乎只是換行位置和
log 用字。後果是「同一個 bug 要改 17 次，而且每次都會漏掉幾支」：
  - 2026-09-14 海外持股價格 0：CH 市場沒對應、港股沒補零（market_utils 修掉）
  - 2026-09-13 假日表少 12 天：17/19 支漏更新，造成真實資料缺漏（tw_calendar 修掉）
  - NaN 防護：同一行 hist[hist["Close"].notna()] 要貼 50 次
  - load_prev_holdings 的 _temp 防護：重構前只有 11/19 支有

本檔把「所有基金都一樣的部分」收成單一實作，各爬蟲只留下真正因資料來源而異
的抓取／解析邏輯（download_xlsx、parse_*、fetch_*）。以後這類 bug 改一次就好。

## 使用方式
    from etf_core import FundConfig, build_data_json, get_price, fmt_zhang
    CFG = FundConfig(code="00980A", manager="游景德")
    wrapper = build_data_json(CFG, today_holdings, prev_holdings, data_date_str,
                              aum_ntd=aum_ntd, units=units)

## 相容性
build_data_json 的輸出與重構前 19 支各自的 generate_data_json 逐鍵比對相同，
以 golden-master 測試驗證（見交接文件「重構」段）。少數刻意統一的差異列在該處。
"""

import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import yfinance as yf

from market_utils import yf_symbol, ccy_of, is_stock_code
# 休市日與交易日判斷只有 tw_calendar 一個來源（假日表曾在 17/19 支各存一份而漏更新）
from tw_calendar import is_trading_day, prev_trading_day, next_trading_day

log = logging.getLogger(__name__)

TPE = timezone(timedelta(hours=8))


def today_tw():
    """台灣時間的今天（伺服器時區不一定是 +8，一律顯式換算）。"""
    return datetime.now(TPE).date()


# ============================================================================
# 基金設定
# ============================================================================

class FundConfig:
    """一支基金的全部差異都收在這裡；行為分支一律用具名旗標，不用 if code == 判斷。

    code          ETF 代號，如 00980A
    name          中文名（部分爬蟲只在通知用）
    manager       經理人；build_data_json 的 manager 參數可覆寫（中信／安聯是動態抓的）
    holdings_dir  持股 JSON 目錄
    data_file     輸出檔；預設 data_{code}.json
    ipo_date/ipo_price
                  有設定時：掛牌當年度的 YTD 以發行價為基準。period="ytd" 的第一根
                  是掛牌日收盤、不是發行價，對年中掛牌的基金會低估漲幅。
                  未設定則沿用「YTD 第一根收盤」。
    units_are_zhang
                  True 表示呼叫端給的 units 已經是「張」（中信／安聯），否則是「股」。
    aum_yf_fallback
                  抓不到官方規模時，改用 yfinance 的 totalAssets 反推。
    aum_carry_prev
                  抓不到規模時沿用前一交易日數值。
    aum_derive_check
                  額外校驗：若官方張數與 aum/price 反推值差 50% 以上，改用反推值
                  （00987A 的來源張數偶爾是錯的）。
    has_asset_alloc
                  meta 要不要放 assetAllocation（統一 ezmoney 系列才有現金／期貨／附買回）。
    yf_suffix     ETF 自身報價的 yfinance 後綴。None＝先試 .TW 再試 .TWO；
                  上櫃掛牌的基金請明確填 ".TWO"（如 00411A），不要靠 fallback 猜：
                  萬一 .TW 回了殘留或錯誤資料，就會取到錯的價格。
    has_futures   持股清單可能含期貨部位（安聯 00993A 的台指期）。期貨不查價
                  （yfinance 沒有 TX），並標記 isFutures 讓 daily_digest 排除在個股統計外。
    """

    def __init__(self, code, name="", manager="", holdings_dir="holdings",
                 data_file=None, ipo_date=None, ipo_price=None,
                 units_are_zhang=False, aum_yf_fallback=False,
                 aum_carry_prev=True, aum_derive_check=False,
                 has_asset_alloc=False, has_futures=False, yf_suffix=None):
        self.code = code
        self.name = name
        self.manager = manager
        self.holdings_dir = holdings_dir
        self.data_file = data_file or f"data_{code}.json"
        self.ipo_date = ipo_date
        self.ipo_price = ipo_price
        self.units_are_zhang = units_are_zhang
        self.aum_yf_fallback = aum_yf_fallback
        self.aum_carry_prev = aum_carry_prev
        self.aum_derive_check = aum_derive_check
        self.has_asset_alloc = has_asset_alloc
        self.has_futures = has_futures
        self.yf_suffix = yf_suffix


# ============================================================================
# 股價 / 匯率
# ============================================================================

_FX_CACHE = {}


def _drop_nan(hist):
    """去掉 Close 為 NaN 的列。盤中抓 yfinance 會拿到當天還沒收盤的 NaN 列，
    直接用 iloc[-1] 會讓 NaN 流進 JSON，整個網頁會因非法 JSON 掛掉（2026-09 事故兩次）。"""
    return hist[hist["Close"].notna()] if not hist.empty else hist


def fx_to_twd(ccy):
    """1 單位外幣 = ? 台幣。抓不到回 0 → 該股金額視為 0，不污染統計。"""
    if ccy == "TWD":
        return 1.0
    if ccy in _FX_CACHE:
        return _FX_CACHE[ccy]
    rate = 0.0
    try:
        hist = _drop_nan(yf.Ticker(f"{ccy}TWD=X").history(period="5d", timeout=10))
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1])
    except Exception:
        pass
    _FX_CACHE[ccy] = rate
    return rate


def get_price(code_str):
    """最新收盤價，統一換算為新台幣（diffAmount 才能跨幣別加總）。

    台股：無市場後綴，先試 .TW 再試 .TWO（上櫃，如 00411A）。
    海外：TICKER＋市場 格式，交給 market_utils.yf_symbol 處理
          （CH→.SS/.SZ、HK 補零），再乘以當日匯率。
    抓不到一律回 0.0。
    """
    parts = str(code_str).strip().split()
    base = parts[0]
    if len(parts) == 1:
        for suffix in (".TW", ".TWO"):
            try:
                hist = _drop_nan(yf.Ticker(f"{base}{suffix}").history(period="1d", timeout=10))
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return 0.0
    market = parts[1].upper()
    try:
        hist = _drop_nan(yf.Ticker(yf_symbol(base, market)).history(period="1d", timeout=10))
        if hist.empty:
            return 0.0
        local_price = float(hist["Close"].iloc[-1])
    except Exception:
        return 0.0
    fx = fx_to_twd(ccy_of(market))
    return round(local_price * fx, 2) if fx > 0 else 0.0


# ============================================================================
# 持股檔案
# ============================================================================

def holdings_path(cfg, date_str):
    return os.path.join(cfg.holdings_dir, f"{cfg.code}_holdings_{date_str}.json")


def holdings_exist_for(cfg, date_str):
    return os.path.exists(holdings_path(cfg, date_str))


def load_prev_holdings(cfg, exclude_date_str):
    """讀「排除指定日期後最新的一份」持股 JSON。

    排除 _temp 是必要的：下載中途的暫存檔若被當成前一日持股，
    整份加減碼統計都會錯。重構前只有 11/19 支有這個防護。
    """
    pattern = os.path.join(cfg.holdings_dir, f"{cfg.code}_holdings_*.json")
    prev_files = [f for f in sorted(glob.glob(pattern))
                  if exclude_date_str not in os.path.basename(f) and "_temp" not in f]
    if prev_files:
        log.info(f"Previous holdings: {os.path.basename(prev_files[-1])}")
        with open(prev_files[-1], "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("No previous holdings file found.")
    return []


def save_holdings(cfg, date_str, holdings):
    """寫當日持股 JSON。os.replace 是原子操作，避免程式中途被砍留下半份檔案。"""
    path = holdings_path(cfg, date_str)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)
    log.info(f"Saved {os.path.basename(path)}（{len(holdings)} 檔）")
    return path


# ============================================================================
# 格式化
# ============================================================================

def fmt_zhang(shares):
    """股數 → 張（1 張 = 1000 股），帶正號。"""
    zhang = shares / 1000
    sign = "+" if zhang > 0 else ""
    if zhang == int(zhang):
        return f"{sign}{int(zhang):,}張"
    return f"{sign}{zhang:,.1f}張"


def fmt_money(amount):
    """元 → 「1.23億」或「8,400萬」（取絕對值，正負號由呼叫端決定）。"""
    a = abs(amount)
    if a < 5e3:            # 不到 0.5 萬就四捨五入成 0，別顯示成「0萬」
        return "0"
    return f"{a / 1e8:,.2f}億" if a >= 1e8 else f"{a / 1e4:,.0f}萬"


# ============================================================================
# 買賣超金額（單檔通知與台股/海外日報共用，兩邊數字才對得起來）
# ============================================================================

def trade_amount(h, meta):
    """該持股當日的變動金額（元，正=買、負=賣）。

    優先用 diffAmount；若為 0 但有股數變動，代表 yfinance 抓不到價（常見於上櫃/小型股）
    → 用官方權重×淨資產回推單價估算，修正金額被系統性低估的偏差。
    （原本是 daily_digest._best_amount，2026-09-18 搬來這裡讓單檔通知共用。）
    """
    ds = h.get("diffShares", 0)
    if ds == 0:
        return 0.0
    amt = h.get("diffAmount", 0) or 0
    if amt != 0:
        return float(amt)
    price = h.get("price", 0) or 0
    if price <= 0:
        aum_now = (meta.get("totalMarketCap") or 0) * 1e8
        aum_prev = (meta.get("prevTotalMarketCap") or 0) * 1e8
        if h.get("shares", 0) > 0 and h.get("todayWeight", 0) > 0 and aum_now > 0:
            price = (h["todayWeight"] / 100) * aum_now / h["shares"]
        elif h.get("prevShares", 0) > 0 and h.get("yestWeight", 0) > 0:
            base = aum_prev or aum_now
            if base > 0:
                price = (h["yestWeight"] / 100) * base / h["prevShares"]
    return ds * price


def is_first_day(holdings):
    """剛納入追蹤（或剛掛牌）的第一天：>80% 現有持股的前一日股數為 0。
    這天整份持股都會被當成「新增買入」，不能當成真的買超（與日報的判定相同）。"""
    active = [h for h in holdings if h.get("shares", 0) > 0]
    return bool(active) and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8


def trade_totals(holdings, meta):
    """(買超, 賣超) 元；賣超為負數。期貨（如 00993A 的 TX）與非股票部位不計入。"""
    buy = sell = 0.0
    for h in holdings:
        if h.get("isFutures") or not is_stock_code(h.get("code", "")):
            continue
        a = trade_amount(h, meta)
        if a > 0:
            buy += a
        elif a < 0:
            sell += a
    return buy, sell


def format_trade_line(wrapper):
    """單檔通知的買賣超金額行，例：💹 買超 1.23億　賣超 8,400萬　淨 +3,900萬

    買超＝新增＋加碼、賣超＝減碼＋出清，以當日收盤價計（海外持股已換算新台幣）。
    基金有大額申購/贖回時，持股增減有一部分是被動的（通知的「基金規模」行會標示）。
    """
    hs, meta = wrapper.get("holdings", []), wrapper.get("meta", {})
    if is_first_day(hs):
        return "💹 買賣超：首日建倉（整份持股皆為新進），不列計"
    buy, sell = trade_totals(hs, meta)
    if not buy and not sell:
        return "💹 買賣超：今日無持股異動"
    net = buy + sell
    net_s = fmt_money(net)
    sign = "" if net_s == "0" else ("+" if net > 0 else "-")
    return f"💹 買超 {fmt_money(buy)}　賣超 {fmt_money(sell)}　淨 {sign}{net_s}"


# ============================================================================
# ETF 本身的報價 / YTD
# ============================================================================

def _etf_hist(code, pinned=None):
    """ETF 自己的 YTD 歷史。有 pinned（cfg.yf_suffix）就只用它；否則先試 .TW 再試 .TWO。
    上櫃掛牌的基金在 .TW 查無資料，若不處理，股價與 YTD 會整欄變 0（00411A 曾因此顯示 0 元）。"""
    for suffix in ((pinned,) if pinned else (".TW", ".TWO")):
        try:
            hist = _drop_nan(yf.Ticker(f"{code}{suffix}").history(period="ytd", timeout=10))
            if not hist.empty:
                return hist
        except Exception:
            pass
    return None


def etf_quote(cfg):
    """回傳 (ytd_str, etf_price, price_change_pct, prev_price)；任何失敗都回預設值。"""
    ytd_val, etf_price, price_change, prev_price = "0.00", 0.0, 0.0, 0.0
    try:
        hist = _etf_hist(cfg.code, cfg.yf_suffix)
        if hist is not None and len(hist) >= 1:
            last = float(hist["Close"].iloc[-1])
            etf_price = round(last, 2)
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                prev_price = round(prev, 2)
                price_change = round((last - prev) / prev * 100, 2)
            base = None
            if cfg.ipo_date and cfg.ipo_price and datetime.now(TPE).year == int(cfg.ipo_date[:4]):
                base = float(cfg.ipo_price)      # 掛牌當年度以發行價為基準
            elif len(hist) >= 2:
                base = float(hist["Close"].iloc[0])
            if base:
                ytd_val = f"{(last - base) / base * 100:.2f}"
            log.info(f"ETF Price: {etf_price}, YTD: {ytd_val}% (base {base})")
    except Exception as e:
        log.warning(f"ETF price/YTD fetch failed: {e}")
    return ytd_val, etf_price, price_change, prev_price


# ============================================================================
# 主體：產生 data_XXXXXA.json
# ============================================================================

def build_data_json(cfg, today_holdings, prev_holdings, data_date_str,
                    aum_ntd=0, units=0, asset_alloc=None, manager=None,
                    price_fn=None):
    """比較今日與前一日持股、查價、寫出 data_{code}.json，回傳 wrapper dict。

    price_fn 只為測試保留；正式執行一律用本模組的 get_price。
    """
    price_of = price_fn or get_price
    prev_dict = {h["code"]: h for h in prev_holdings}

    # 前一次的收盤價：網頁要顯示昨收，而查價當下拿不到歷史價，只能沿用上一份 JSON。
    prev_prices_map = {}
    if os.path.exists(cfg.data_file):
        try:
            with open(cfg.data_file, "r", encoding="utf-8") as f:
                for ph in json.load(f).get("holdings", []):
                    if ph.get("price", 0) > 0:
                        prev_prices_map[ph["code"]] = ph["price"]
        except Exception:
            pass

    def _price(code, is_futures):
        # 期貨（如安聯的 TX 台指期）yfinance 查不到 → 一律 0，且不可混進個股統計。
        return 0.0 if is_futures else price_of(code)

    final_output = []
    total = len(today_holdings)
    log.info(f"Fetching prices for {total} holdings...")

    for i, h in enumerate(today_holdings):
        code = h["code"]
        prev_data = prev_dict.get(code, {})
        shares_prev = prev_data.get("shares", 0)
        diff_shares = h["shares"] - shares_prev
        # 來源解析階段用 is_futures（底線），寫進 JSON 後叫 isFutures（駝峰），兩種都要認
        is_fut = bool(h.get("is_futures") or h.get("isFutures"))
        price = _price(code, is_fut)
        row = {
            "code": code, "name": h["name"],
            "shares": h["shares"], "prevShares": shares_prev,
            "price": round(price, 2),
            "prevPrice": prev_prices_map.get(code, 0),
            "yestWeight": prev_data.get("weight", 0.0), "todayWeight": h["weight"],
            "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
        }
        if cfg.has_futures:
            row["isFutures"] = is_fut
        final_output.append(row)
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i + 1}/{total}")

    # 今日清單裡消失的 → 視為出清，股數 0、權重 0
    today_codes = {h["code"] for h in today_holdings}
    for prev_h in prev_holdings:
        if prev_h["code"] in today_codes:
            continue
        code = prev_h["code"]
        is_fut = bool(prev_h.get("is_futures") or prev_h.get("isFutures"))
        price = _price(code, is_fut)
        diff_shares = -prev_h["shares"]
        row = {
            "code": code, "name": prev_h["name"],
            "shares": 0, "prevShares": prev_h["shares"],
            "price": round(price, 2),
            "prevPrice": prev_prices_map.get(code, 0),
            "yestWeight": prev_h.get("weight", 0.0), "todayWeight": 0.0,
            "diffShares": diff_shares, "diffAmount": round(diff_shares * price, 2),
        }
        if cfg.has_futures:
            row["isFutures"] = is_fut
        final_output.append(row)

    final_output.sort(key=lambda x: x["todayWeight"], reverse=True)
    for idx, item in enumerate(final_output):
        item["rank"] = idx + 1

    ytd_val, etf_price, price_change, prev_price = etf_quote(cfg)

    # ---- 基金規模 ----
    total_market_cap = round(aum_ntd / 1e8, 2) if aum_ntd > 0 else 0.0
    if cfg.units_are_zhang:
        total_shares_zhang = units if units > 0 else 0
    else:
        raw = units if units > 0 else (round(aum_ntd / etf_price) if aum_ntd > 0 and etf_price > 0 else 0)
        if cfg.aum_derive_check and aum_ntd > 0 and etf_price > 0:
            # 來源張數偶爾是錯的：與 規模/淨值 反推值差 50% 以上就不信它
            derived = round(aum_ntd / etf_price)
            if raw <= 0 or abs(raw - derived) / derived > 0.5:
                raw = derived
        total_shares_zhang = raw // 1000

    if total_shares_zhang == 0 and cfg.aum_yf_fallback:
        try:
            assets = float(yf.Ticker(f"{cfg.code}{cfg.yf_suffix or '.TW'}").info.get("totalAssets") or 0)
            if assets > 0 and etf_price > 0:
                total_shares_zhang = round(assets / etf_price) // 1000
                total_market_cap = round(assets / 1e8, 2)
        except Exception:
            pass

    # 只跟前一個交易日比，避免腳本跳日造成跨多天誤差
    prev_total_shares, prev_total_market_cap = 0, 0.0
    if os.path.exists(cfg.data_file):
        try:
            with open(cfg.data_file, "r", encoding="utf-8") as f:
                prev_meta = json.load(f).get("meta", {})
            ptd = prev_trading_day(datetime.strptime(data_date_str, "%Y-%m-%d").date()).strftime("%Y-%m-%d")
            if prev_meta.get("dataDate", "") == ptd:
                prev_total_shares = prev_meta.get("totalShares", 0)
                prev_total_market_cap = prev_meta.get("totalMarketCap", 0.0)
            else:
                log.info(f"規模比較跳過：JSON dataDate={prev_meta.get('dataDate')} 非前一交易日({ptd})")
        except Exception:
            pass

    # 合理性驗證：與前一交易日差距過大視為來源解析異常，捨棄新值
    if total_shares_zhang > 0 and prev_total_shares > 0:
        ratio = total_shares_zhang / prev_total_shares
        if ratio < 0.1 or ratio > 5.0:
            log.warning(f"規模異常：totalShares={total_shares_zhang} 與前一交易日 "
                        f"{prev_total_shares} 相差 {ratio:.1%}，改用前一交易日數值")
            total_shares_zhang, total_market_cap = prev_total_shares, prev_total_market_cap

    if total_shares_zhang == 0 and prev_total_shares > 0 and cfg.aum_carry_prev:
        total_shares_zhang = prev_total_shares
        total_market_cap = (round(etf_price * prev_total_shares * 1000 / 1e8, 2)
                            if etf_price > 0 else prev_total_market_cap)

    meta = {
        "manager": manager or cfg.manager,
        "ytd": ytd_val, "etfPrice": etf_price,
        "priceChange": price_change, "prevPrice": prev_price,
        "dataDate": data_date_str,
        "lastUpdate": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "totalShares": total_shares_zhang,
        "prevTotalShares": prev_total_shares,
        "totalMarketCap": total_market_cap,
        "prevTotalMarketCap": prev_total_market_cap,
    }
    if cfg.has_asset_alloc:
        meta["assetAllocation"] = asset_alloc or {}

    wrapper = {"meta": meta, "holdings": final_output}
    tmp = cfg.data_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=4)
    os.replace(tmp, cfg.data_file)
    log.info(f"{cfg.data_file} updated: {len(final_output)} holdings, "
             f"{total_shares_zhang:,}張, {total_market_cap}億")
    return wrapper
