# -*- coding: utf-8 -*-
"""
allianz.py — 安聯投信 ETF 專區（etf.allianzgi.com.tw）的抓取與解析。
00412A（E0004）與 00402A（E0003）共用；00993A（E0002）寫得比較早，仍用自己的一份
（它的 Table[2] 是台指期貨，解析規則不同）。

## 來源特性
- API 有 CSRF 保護，直接 HTTP 會 400 → Playwright 開頁面、攔截 GetFundAssets / GetFundDetail。
- API 只回「最新一天」，不接受日期參數（實測 SearchDate / Date / NavDate 皆被忽略）。
  所以只要出現沒存過的持股日就存下來 —— 錯過的那天之後補不回來。
- 表格依標題辨識，不可用位置：00993A 的 Table[2] 是台指期貨、00412A 的 Table[2] 是
  「附買回債券」（只有金額沒有股數）、00402A 只有股票一張表。只收標題以「股票」開頭的表。
- 陸股用 Bloomberg 交易所碼 C1（上海）/ C2（深圳），一律轉成 CH：下游的
  market_utils.yf_symbol、is_stock_code 與日報只認得 CH，不轉的話查不到價、也不算個股。

## 持股日（每支基金依持股市場而定，見 expected_date / holdings_date 的 rule）
FundAsset 有兩個日期：NavDate（頁面上的「資料日期」＝淨值日）與 PCFDate（申購買回清單適用日）。
安聯在 T 日收盤後公布 PCFDate＝T+1 的清單；日美股 T 日還沒收盤，淨值只算到 T-1，所以
NavDate＝T-1。股數則是「公布當下」的部位：
  rule="pcf"  持股日＝PCFDate 的前一交易日（＝公布當天 T）。持股以台股／亞洲為主，T 日的
              交易已反映在股數裡（00412A；2026-10-14 使用者確認：開盤時頁面為 NavDate 10/12、
              PCFDate 10/14 → 這是 10/13 的持股，收盤後才有 10/14）。
  rule="nav"  持股日＝NavDate（T-1）。純美股：公布時美股 T 日還沒開盤，部位就是美股 T-1
              收盤後的，與淨值日相同（安聯自己的折溢價表也把 10/13 淨值配美股 10/13 收盤）；
              也與其他海外 ETF「資料比台股晚一個交易日」的慣例一致（00402A）。
基金規模（Aum / Units）兩種都是 NavDate 那天的數字。
"""

import logging
import re
from datetime import datetime

from playwright.sync_api import sync_playwright

from tw_calendar import prev_trading_day, next_trading_day, last_trading_day

log = logging.getLogger(__name__)

PAGE_URL = "https://etf.allianzgi.com.tw/etf-info/{fund_no}?tab={tab}"

# Bloomberg 的中國交易所碼 → 本專案統一的市場碼
MARKET_ALIAS = {"C1": "CH", "C2": "CH"}

DATE_RULES = ("pcf", "nav")


def fetch_fund_assets(fund_no):
    """開安聯 ETF 頁面，攔截 GetFundAssets（持股＋規模）與 GetFundDetail（經理人）。
    回傳 (raw GetFundAssets dict 或 None, 經理人 或 None)。"""
    captured = {}

    def handle_response(response):
        url = response.url
        if "GetFundAssets" in url:
            try:
                captured["data"] = response.json()
                log.info(f"Captured holdings API: {url}")
            except Exception as e:
                log.warning(f"Failed to parse holdings response: {e}")
        elif "GetFundDetail" in url:
            try:
                manager = response.json().get("Entries", {}).get("CManager", "")
                if manager:
                    captured["manager"] = manager
                    log.info(f"Captured manager: {manager}")
            except Exception as e:
                log.warning(f"Failed to parse GetFundDetail response: {e}")

    url = PAGE_URL.format(fund_no=fund_no, tab=4)
    log.info(f"Launching Playwright to fetch {url} ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="zh-TW",
        )
        page = context.new_page()
        page.on("response", handle_response)
        try:
            # tab=1 觸發 GetFundDetail（經理人），tab=4 觸發 GetFundAssets（持股）
            page.goto(PAGE_URL.format(fund_no=fund_no, tab=1), wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4000)
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(8000)
        except Exception as e:
            log.warning(f"Page load issue (non-fatal): {e}")
        browser.close()

    return captured.get("data"), captured.get("manager")


def normalize_code(code_raw):
    """'600183 C1' → '600183 CH'；'6857 JP'、'NVDA US'、'2330' 原樣。"""
    parts = code_raw.split()
    if len(parts) == 2:
        market = parts[1].upper()
        return f"{parts[0]} {MARKET_ALIAS.get(market, market)}"
    return code_raw.strip()


def _parse_date(s):
    try:
        return datetime.strptime(str(s).replace("/", "-"), "%Y-%m-%d").date()
    except ValueError:
        return None


def holdings_date(fa, rule):
    """依 rule 由 FundAsset 的 NavDate / PCFDate 推出持股日（見檔頭）；推不出來回傳 ''。"""
    assert rule in DATE_RULES, rule
    pcf, nav = _parse_date(fa.get("PCFDate", "")), _parse_date(fa.get("NavDate", ""))
    if nav and pcf and next_trading_day(nav) != prev_trading_day(pcf):
        log.warning(f"NavDate {nav} 與 PCFDate {pcf} 的間隔不是預期的兩個交易日")
    if rule == "nav":
        d = nav or (prev_trading_day(prev_trading_day(pcf)) if pcf else None)
    else:
        d = prev_trading_day(pcf) if pcf else (next_trading_day(nav) if nav else None)
    return d.strftime("%Y-%m-%d") if d else ""


def expected_date(run_date, rule):
    """收盤後（排程 18:00~21:00）應該拿到的持股日。"""
    assert rule in DATE_RULES, rule
    t = last_trading_day(run_date)
    return t if rule == "pcf" else prev_trading_day(t)


def parse_fund_assets(raw_data, rule):
    """回傳 (持股日 YYYY-MM-DD, 淨資產 NTD, 在外流通單位數, holdings[])。"""
    try:
        entries = raw_data["Entries"]["Data"]
        fa = entries["FundAsset"]
        tables = entries["Table"]
    except (KeyError, TypeError) as e:
        log.error(f"Unexpected API structure: {e}")
        return "", 0, 0, []

    try:
        aum_ntd = int(float(str(fa.get("Aum", "0")).replace(",", "")))
        units = int(float(str(fa.get("Units", "0")).replace(",", "")))
    except ValueError as e:
        log.warning(f"FundAsset parse error: {e}")
        aum_ntd, units = 0, 0
    data_date = holdings_date(fa, rule)
    log.info(f"NavDate {fa.get('NavDate')}、PCFDate {fa.get('PCFDate')} → 持股日 {data_date}（rule={rule}）")

    holdings, stock_tables = [], 0
    for t in tables:
        title = str(t.get("TableTitle") or "").strip()
        if not title.startswith("股票"):
            if title:
                log.info(f"略過非股票表格：{title}（{len(t.get('Rows', []))} 列）")
            continue
        stock_tables += 1
        # 每列：[序號, 股票代號, 股票名稱, 股數, 權重%]
        for row in t.get("Rows", []):
            if len(row) < 5:
                continue
            code = normalize_code(str(row[1]))
            if not re.match(r"^[0-9A-Za-z]", code):
                continue
            try:
                shares = int(float(str(row[3]).replace(",", "")))
                weight = float(str(row[4]).replace("%", "").replace(",", ""))
            except ValueError:
                continue
            if shares <= 0:
                continue
            holdings.append({"code": code, "name": str(row[2]).strip(), "shares": shares, "weight": weight})

    if stock_tables == 0:
        log.error(f"找不到「股票」表格，版面可能變動。表格標題：{[t.get('TableTitle') for t in tables]}")
    log.info(f"持股日 {data_date}；AUM {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億)；Units {units:,}；持股 {len(holdings)} 檔")
    return data_date, aum_ntd, units, holdings
