"""
tsit.py
-------
台新投信 ETF 頁面（tsit.com.tw/ETF/Home/ETFSeriesDetail/{代號}）的抓取與解析，00987A、00416A 共用。

- 純靜態 HTML，urllib 直接抓，不需 Playwright
- 持股表：代號帶 " TT" 後綴（解析時去掉），用「4 碼數字」辨識持股列；掃描全部 <tr>，不限表格編號
- 持股日：頁面「日期」(PUB_DATE) 是 T+1 申購買回清單日，清單內容是 T 日收盤後部位，
  所以取「YYYY/M/D每基數實際申購總價金」的日期當持股日（parse_holdings_date）。
  呼叫端拿它跟今天比，不同就是官網還沒換日，不能寫入
- 規模：「基金淨資產價值(元)」與「已發行受益權單位總數」；後者的數字在 <td> 後面有換行縮排
"""

import logging
import re
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger(__name__)

URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/{code}"


class HoldingsTableParser(HTMLParser):
    """Scans ALL table rows — any row whose first cell looks like a stock code
    (4-digit number optionally followed by ' TT') is treated as a holding."""

    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_td = False
        self.current_row = []
        self.current_cell = ""
        self.holdings = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_tr = True
            self.current_row = []
        if self.in_tr and tag == "td":
            self.in_td = True
            self.current_cell = ""

    def handle_endtag(self, tag):
        if tag == "tr":
            self.in_tr = False
            if len(self.current_row) >= 4:
                self._process_row(self.current_row)
        if self.in_tr and tag == "td":
            self.in_td = False
            self.current_row.append(self.current_cell.strip())

    def handle_data(self, data):
        if self.in_td:
            self.current_cell += data

    def _process_row(self, row):
        try:
            code = row[0].replace(" TT", "").strip()
            if not re.fullmatch(r'\d{4}', code):
                return
            name = row[1].strip()
            shares = int(float(row[2].replace(",", "").strip()))
            weight = float(row[3].replace("%", "").strip())
            self.holdings.append({"code": code, "name": name, "shares": shares, "weight": weight})
        except Exception:
            pass


def fetch_page(code):
    url = URL.format(code=code)
    log.info(f"Fetching {url} ...")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return None


def parse_holdings(html):
    parser = HoldingsTableParser()
    parser.feed(html)
    log.info(f"Parsed {len(parser.holdings)} holdings from page")
    return parser.holdings


def parse_holdings_date(html):
    """持股日 = 「2026/11/2每基數實際申購總價金」的日期（YYYY-MM-DD）；找不到回傳 None。"""
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})\s*每基數實際申購總價金', html)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_aum(html):
    """(基金淨資產價值 元, 已發行受益權單位數)；抓不到的欄位為 0。"""
    aum_ntd, units = 0, 0
    try:
        m = re.search(r'基金淨資產價值.*?TWD\s*([\d,]+)', html, re.DOTALL)
        if m:
            aum_ntd = int(m.group(1).replace(",", ""))
        m2 = re.search(r'已發行受益權單位總數.*?<td[^>]*>\s*([\d,]+)', html, re.DOTALL)
        if m2:
            units = int(m2.group(1).replace(",", ""))
        log.info(f"AUM from HTML: {aum_ntd:,} NTD ({aum_ntd/1e8:.2f}億), Units: {units:,}")
    except Exception as e:
        log.warning(f"AUM parse failed: {e}")
    return aum_ntd, units


def fetch(code, expected_date):
    """抓頁面並驗證持股日。回傳 (holdings, aum_ntd, units)；官網尚未換到 expected_date 時 holdings 為 None。"""
    html = fetch_page(code)
    if not html:
        return None, 0, 0
    holdings = parse_holdings(html)
    aum_ntd, units = parse_aum(html)
    page_date = parse_holdings_date(html)
    if holdings and page_date != expected_date:
        log.info(f"官網持股日 {page_date} ≠ {expected_date}，尚未更新")
        return None, aum_ntd, units
    return holdings or None, aum_ntd, units
