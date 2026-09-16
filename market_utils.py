# -*- coding: utf-8 -*-
"""
market_utils.py — 海外持股代號 → yfinance 代號與計價幣別（各海外爬蟲共用）。

持股代號為「TICKER 市場」格式，例如 'NVDA US'、'285A JP'、'000660 KS'、
'300757 CH'、'992 HK'。台股代號無市場後綴，由各爬蟲自行處理（.TW / .TWO）。

特殊規則（2026-09-15 修正）：
  - CH（中國 A 股）：6/9 開頭為上海 → .SS，其餘（0/2/3 開頭）為深圳 → .SZ；幣別 CNY。
  - HK（港股）：yfinance 需補零成 4 位數（992 → 0992.HK）。
  先前各爬蟲沒有 CH、港股也沒補零，00988A 的 300757 CH / 992 HK 因此長期被當成股價 0，
  這兩檔的加減碼金額一直以 0 計入報告。
"""

YF_SUFFIX = {
    "US": "", "JP": ".T", "KS": ".KS", "KP": ".KS", "KQ": ".KQ",
    "HK": ".HK", "GY": ".DE", "GR": ".DE", "FP": ".PA", "LN": ".L",
    "SG": ".SI", "NA": ".AS",
}

CCY = {
    "US": "USD", "JP": "JPY", "KS": "KRW", "KP": "KRW", "KQ": "KRW",
    "HK": "HKD", "GY": "EUR", "GR": "EUR", "FP": "EUR", "NA": "EUR",
    "LN": "GBP", "SG": "SGD", "CH": "CNY",
}


def yf_symbol(base, market):
    """'300757','CH' → '300757.SZ'；'992','HK' → '0992.HK'；'NVDA','US' → 'NVDA'。"""
    m = market.upper()
    if m == "CH":
        return f"{base}.SS" if base[:1] in ("6", "9") else f"{base}.SZ"
    if m == "HK":
        return f"{base.zfill(4)}.HK" if base.isdigit() else f"{base}.HK"
    return f"{base}{YF_SUFFIX.get(m, '')}"


def ccy_of(market):
    """市場碼 → 計價幣別；未知市場預設 USD。"""
    return CCY.get(market.upper(), "USD")


# 非股票部位（期貨、現金等）不可混進個股統計。
# 00993A 的持股清單含一筆 'TX 台指期貨'（19 口、權重約 1~2%），yfinance 無此代號 → 價格 0；
# 2026-05-15 它口數變動時，日報把它當成「新建倉」的股票列出，
# 且 daily_digest._best_amount 會在 diffAmount=0 但股數有變動時用『權重×淨資產』回推金額，
# 可能報出憑空推算的巨額「台指期貨」交易。
import re as _re

_TW_STOCK = _re.compile(r'^[0-9]{4,6}[A-Z]?$')


def is_stock_code(code):
    """判斷是否為個股代號。台股為 4~6 碼數字(+英文)；海外為「TICKER 市場」且市場碼已知。

    期貨（TX）、現金等非股票部位一律回傳 False，不納入個股統計。
    """
    c = str(code or '').strip()
    if not c:
        return False
    parts = c.split()
    if len(parts) == 1:
        return bool(_TW_STOCK.match(c))
    if len(parts) == 2:
        return parts[1].upper() in YF_SUFFIX or parts[1].upper() in CCY
    return False
