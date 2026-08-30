"""
daily_digest.py
----------------
每日收盤後，待所有台股 ETF 持股更新完成，自動產生「主動 ETF 經理人都在買什麼」
摘要訊息並發送 Telegram。

發送條件（避免重複發送與資料不完整就發送）：
1. last_digest.txt 記錄的日期 != 今天（當日尚未發送過）
2. 台股 ETF 中 dataDate == 今天的數量 >= SEND_THRESHOLD
   或 TW 時間已過 21:00 且至少 6 檔更新（部分來源當日故障時的 fallback）

統計規則：
- 00988A（海外 T+1）不列入當日統計
- 「首日 ETF」（>80% 持股 prevShares=0，例如剛納入追蹤的新 ETF）不列入
  買賣金額統計，避免整個投資組合被當成「新增買入」的雜訊
- 共識升溫/退潮：與前一交易日 snapshot 比較各股「加碼 ETF 家數」
"""

import glob
import json
import os
import sys
import logging
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DIGESTS_FILE = "digests.json"   # 供網站「每日分析」分頁讀取（{date: text}）
SITE_URL = "https://wuminwu.github.io/woody-etf-tracker/"
PROVENANCE = "報告來源: 854-Woody (狼群專用未經同意請勿轉傳，若數據有誤請通知我)"

# 台股 ETF（純台股；海外/混合另成一組）
TW_ETFS = [
    ("00981A", "統一台股增長"),
    ("00400A", "國泰動能高息"),
    ("00403A", "統一升級50"),
    ("00980A", "野村智慧優選"),
    ("00985A", "野村台灣50"),
    ("00991A", "復華未來50"),
    ("00992A", "群益科技創新"),
    ("00982A", "群益台灣強棒"),
    ("00987A", "台新優勢成長"),
    ("00993A", "安聯台灣"),
    ("00995A", "中信台灣卓越"),
    ("00996A", "兆豐台灣豐收"),
    ("00405A", "富邦台灣龍耀"),
]

# 海外／台美混合 ETF（持股含美股，T+1 更新；未來新增混合型 ETF 加在這裡即可）
OVERSEAS_ETFS = [
    ("00988A", "統一全球創新"),
    ("00997A", "群益美國增長"),
    ("00990A", "元大全球AI新經濟"),
]

# 報告群組設定。tw 用「今天」為報告日；overseas 因 T+1，用該組目前最新的資料日。
GROUPS = {
    "tw": {
        "etfs": TW_ETFS,
        "title": "📊 {md} 主動 ETF 經理人都在買什麼（台股）",
        "subhead": "（{u}/{t} 檔已更新持股；海外/混合 ETF 另發一份）",
        "marker": "last_digest.txt",
        "digests_file": "digests.json",
        "use_today": True,
        # 門檻：21:00 前需 ≥10 檔；之後放寬為 ≥6 檔
        "threshold_std": 10, "fallback_hour": 21, "threshold_fallback": 6,
    },
    "overseas": {
        "etfs": OVERSEAS_ETFS,
        "title": "🌍 {md} 主動 ETF 經理人都在買什麼（海外/混合）",
        "subhead": "（{u}/{t} 檔海外/混合 ETF；持股 T+1，各檔取最新揭露日）",
        "marker": "last_digest_overseas.txt",
        "digests_file": "digests_overseas.json",
        "use_today": False,   # 用該組最新資料日（T+1）
        "threshold_std": 1, "fallback_hour": 0, "threshold_fallback": 1,
    },
}

# 個股 → 產業對照（涵蓋主動式 ETF 常見持股；查不到歸「其他」）。新標的常出現時可補。
SECTOR_MAP = {
    # 半導體上游/晶圓/IC設計/IP
    "2330": "半導體", "2303": "半導體", "2454": "IC設計", "2379": "IC設計",
    "3034": "IC設計", "3443": "IC設計", "5274": "IC設計", "3035": "IC設計",
    "8299": "IC設計", "6531": "IC設計", "4961": "IC設計", "3661": "IC設計",
    "2344": "記憶體", "2408": "記憶體", "3006": "記憶體",
    # 封測/測試介面
    "3711": "封測", "6239": "封測", "6147": "封測", "3264": "封測",
    "6515": "測試介面", "6223": "半導體設備", "3680": "半導體設備", "6271": "封測",
    # PCB/載板/銅箔基板
    "3037": "PCB載板", "2368": "PCB載板", "8046": "PCB載板", "3189": "PCB載板",
    "6269": "PCB載板", "8358": "PCB載板", "2383": "PCB載板", "6274": "PCB載板",
    "3044": "PCB載板", "6213": "PCB載板",
    # 散熱/機殼/機構
    "3017": "散熱", "3653": "散熱", "8210": "伺服器機構", "6669": "伺服器",
    "2376": "伺服器", "2356": "伺服器", "3231": "伺服器", "2382": "伺服器",
    # 被動元件/連接器
    "2327": "被動元件", "2492": "連接器", "3023": "被動元件",
    # 光通訊/矽光子
    "4979": "光通訊", "3450": "光通訊", "3081": "光通訊",
    # 設備/其他電子
    "3008": "光學", "2308": "電源/散熱", "2059": "伺服器機構", "1560": "工具機",
    "6488": "半導體設備", "5483": "半導體", "3105": "記憶體",
    # 金融/傳產
    "2882": "金融", "2891": "金融", "2412": "電信", "1101": "傳產",
}


def _sector(code):
    return SECTOR_MAP.get(str(code).split()[0], "其他")


import time as _time
_last_send_ts = 0.0
_MIN_SEND_GAP = 4.0   # 同一聊天室每則至少間隔（秒）=15 則/分鐘，安全低於 Telegram 約 20 則/分鐘上限。
                      # 先前 1.5 秒=40 則/分鐘（超速 2 倍），週五爆量（日報+週報+單檔共 ~27 則同程序）
                      # 會被 Telegram 靜默丟棄（有時不回 429），導致週報「已發送卻收不到」。


def _split_message(message, limit=3900):
    """把過長訊息以行為界切成多段（Telegram 單則上限 4096 字，留安全邊界）。"""
    if len(message) <= limit:
        return [message]
    parts, buf = [], ""
    for line in message.split("\n"):
        # 單行本身就超長：硬切
        while len(line) > limit:
            if buf:
                parts.append(buf); buf = ""
            parts.append(line[:limit]); line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf); buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        parts.append(buf)
    return parts


def send_telegram(message):
    """發送 Telegram 訊息。過長自動分段 + 主動節流（每則間隔 ≥1.5s）+ 429 洪水保護重試，確保訊息不被靜默丟棄。"""
    parts = _split_message(message)
    if len(parts) > 1:
        n = len(parts)
        ok_all = True
        for i, p in enumerate(parts, 1):
            ok_all = _send_telegram_one(f"（{i}/{n}）\n{p}") and ok_all
        return ok_all
    return _send_telegram_one(message)


def _send_telegram_one(message):
    """實際送出單一則（≤4096 字）。"""
    global _last_send_ts
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set — printing message instead.")
        print(message)
        return False
    payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(4):
        gap = _MIN_SEND_GAP - (_time.time() - _last_send_ts)   # 主動節流
        if gap > 0:
            _time.sleep(gap)
        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                _last_send_ts = _time.time()
                return json.loads(r.read()).get("ok", False)
        except urllib.error.HTTPError as e:
            _last_send_ts = _time.time()
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            if e.code == 429:   # 洪水保護：依 retry_after 等待後重試
                wait = body.get("parameters", {}).get("retry_after", 5) + 1
                log.warning(f"Telegram 429 flood control, 等待 {wait}s 後重試（第 {attempt+1} 次）")
                _time.sleep(wait)
                continue
            log.warning(f"Telegram HTTP {e.code}: {body}")
            return False
        except Exception as e:
            log.warning(f"Telegram failed: {e}")
            _time.sleep(3)
    log.warning("Telegram 多次重試仍失敗。")
    return False


def yi(amount):
    """元 → 億，一位小數。"""
    return f"{amount / 1e8:.1f}"


def find_prev_snapshot(today_str):
    """回傳今天以前最近一個 snapshot 的內容（dict），找不到回傳 None。"""
    files = sorted(glob.glob("snapshots/????-??-??.json"))
    prev = [f for f in files if os.path.basename(f)[:10] < today_str]
    if not prev:
        return None
    path = prev[-1]
    log.info(f"Previous snapshot: {path}")
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return None


def _gather_from_live(date_str, etfs):
    """從 live data_{code}.json 蒐集指定 ETF 群組在 date_str 的 holdings + meta。"""
    etf_data, updated = {}, []
    for code, _name in etfs:
        path = f"data_{code}.json"
        if not os.path.exists(path):
            continue
        d = json.loads(open(path, encoding="utf-8").read())
        if d["meta"].get("dataDate") != date_str:
            continue
        updated.append(code)
        etf_data[code] = {"holdings": d.get("holdings", []), "meta": d.get("meta", {})}
    return etf_data, updated


def _gather_latest(etfs):
    """
    海外/混合組用：各 ETF 取「自己 data_*.json 的最新揭露日」資料（不強制同一天），
    因 00988A / 00997A 等 T+1 來源的資料日常差一天，強制同日會導致報告每次只含一檔。
    回傳 (etf_data, updated, report_date=各檔最新日的最大值)。
    """
    etf_data, updated, dates = {}, [], []
    for code, _name in etfs:
        path = f"data_{code}.json"
        if not os.path.exists(path):
            continue
        d = json.loads(open(path, encoding="utf-8").read())
        dd = d["meta"].get("dataDate")
        if not dd:
            continue
        updated.append(code)
        dates.append(dd)
        etf_data[code] = {"holdings": d.get("holdings", []), "meta": d.get("meta", {})}
    return etf_data, updated, (max(dates) if dates else None)


def _gather_overseas_tw_contributors():
    """
    海外 ETF（OVERSEAS_ETFS）持有的「台股部位」——代號無市場後綴者（如 2330；US/JP 為 'MU US'）。
    供台股報告一併計入個股統計（2330 的加碼家數/淨額要含海外 ETF 的台股部位）。
    回傳 (etf_data, codes)；這些 ETF 不計入台股報告的「X/13 已更新」分母。
    """
    etf_data, codes = {}, []
    for code, _name in OVERSEAS_ETFS:
        path = f"data_{code}.json"
        if not os.path.exists(path):
            continue
        try:
            d = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        tw_holdings = [h for h in d.get("holdings", []) if " " not in str(h.get("code", ""))]
        if tw_holdings:
            etf_data[code] = {"holdings": tw_holdings, "meta": d.get("meta", {})}
            codes.append(code)
    return etf_data, codes


def _filter_snapshot(snap, etfs):
    """只保留群組內 ETF 的 snapshot 區塊，避免跨群組污染共識比較。"""
    if not snap:
        return None
    codes = {c for c, _ in etfs}
    return {k: v for k, v in snap.items() if k in codes}


def _gather_from_snapshot(snap):
    """從 snapshot dict（{etf:{meta,holdings}}）蒐集當日各 TW ETF。"""
    tw_codes = {c for c, _ in TW_ETFS}
    etf_data, updated = {}, []
    for etf_id, blk in snap.items():
        if etf_id not in tw_codes:
            continue
        updated.append(etf_id)
        etf_data[etf_id] = {"holdings": blk.get("holdings", []), "meta": blk.get("meta", {})}
    return etf_data, updated


def _best_amount(h, meta):
    """
    回傳該持股的變動金額（元）。優先用 diffAmount；若為 0 但有股數變動，
    代表 yfinance 抓不到價（常見於上櫃/小型股）→ 用官方權重×淨資產回推單價估算，
    修正金額被系統性低估的偏差。
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


def _recent_snapshot_dirs(ref_date_str, n=12):
    """回傳最近 n 個（含 ref_date）snapshot 的 (date, 加碼set, 減碼set, {code:當日淨額})，升冪。"""
    files = sorted(glob.glob("snapshots/????-??-??.json"))
    files = [f for f in files if os.path.basename(f)[:10] <= ref_date_str][-n:]
    out = []
    for f in files:
        try:
            snap = json.loads(open(f, encoding="utf-8").read())
        except Exception:
            continue
        adds, reds = set(), set()
        net = defaultdict(float)
        for _etf, blk in snap.items():
            for h in blk.get("holdings", []):
                ds = h.get("diffShares", 0)
                net[h["code"]] += h.get("diffAmount", 0) or 0
                if ds > 0:
                    adds.add(h["code"])
                elif ds < 0:
                    reds.add(h["code"])
        out.append((os.path.basename(f)[:10], adds, reds, net))
    return out


def _streak(recent, code, want):
    """從最近日往回算 code 連續被 want('add'/'red') 的天數。"""
    cnt = 0
    for _date, adds, reds, _net in reversed(recent):
        s = adds if want == "add" else reds
        if code in s:
            cnt += 1
        else:
            break
    return cnt


def _streak_cum(recent, code, want):
    """連續同向天數 + 期間累計淨額（元）。"""
    cnt, cum = 0, 0.0
    for _date, adds, reds, net in reversed(recent):
        s = adds if want == "add" else reds
        if code in s:
            cnt += 1
            cum += net.get(code, 0.0)
        else:
            break
    return cnt, cum


def yi_signed(amount):
    """元 → 億，帶正負號，一位小數。"""
    v = amount / 1e8
    return f"{'+' if v >= 0 else ''}{v:.1f}"


def build_digest(ref_date, group="tw"):
    """
    為指定群組產生分析。tw 用 ref_date 為報告日；overseas（T+1）用該組最新資料日。
    回傳 (report_date, message, updated_count)。
    """
    g = GROUPS[group]
    etfs = g["etfs"]
    contributors = []
    if g["use_today"]:
        report_date = ref_date
        etf_data, updated = _gather_from_live(report_date, etfs)
        # 海外 ETF 的台股部位也計入台股報告（額外貢獻者，不算進 X/13 分母）
        contrib_data, contributors = _gather_overseas_tw_contributors()
        etf_data.update(contrib_data)
    else:
        etf_data, updated, report_date = _gather_latest(etfs)
        report_date = report_date or ref_date
    prev_snap = _filter_snapshot(find_prev_snapshot(report_date), etfs)
    msg, cnt = render_digest(report_date, etf_data, updated, prev_snap,
                             total_tracked=len(etfs),
                             title_tmpl=g["title"], subhead_tmpl=g["subhead"],
                             contributors=contributors)
    return report_date, msg, cnt


def render_digest(today_str, etf_data, updated, prev_snap,
                  total_tracked=None, title_tmpl="📊 {md} 主動 ETF 經理人都在買什麼",
                  subhead_tmpl="（{u}/{t} 檔已更新持股）", contributors=()):
    """
    依各 ETF 的 holdings + meta 與前一日 snapshot 產生分析文字。
    etf_data: { code: {"holdings": [...], "meta": {...}} }
    updated:  有當日資料的 ETF 代號清單（含首日 ETF），計入 header 分母
    contributors: 額外貢獻者 ETF（如海外 ETF 的台股部位），計入個股統計但不計 header 分母
    prev_snap: 前一交易日 snapshot dict（已篩成同群組）或 None
    回傳 (message, updated_count)。message 不含結尾網站連結（由發送端另加）。
    """
    first_day = []
    # 個股淨額聚合：code -> {name, add_amt, red_amt(負), add_etfs[], red_etfs[], 權重方向計數}
    stock = defaultdict(lambda: {"name": "", "add_amt": 0.0, "red_amt": 0.0,
                                 "add_etfs": [], "red_etfs": [],
                                 "add_wt_up": 0, "red_wt_dn": 0})
    new_pos, cleared = [], []
    total_buy, total_sell = 0.0, 0.0   # 全市場毛買超 / 毛賣超（賣為負）
    fund_flows = []   # (code, pct) ETF 當日規模(受益權單位)顯著變動 → 申購(+)/贖回(-)

    for code in list(updated) + [c for c in contributors if c in etf_data]:
        holdings = etf_data.get(code, {}).get("holdings", [])
        meta = etf_data.get(code, {}).get("meta", {})
        active = [h for h in holdings if h.get("shares", 0) > 0]
        if active and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8:
            if code in updated:   # 貢獻者（海外台股部位）首日就靜默略過，不列入首日提示
                first_day.append(code)
            continue

        # 規模(受益權單位)變動 → 申購/贖回；只看計入分母的本群組 ETF
        if code in updated:
            ts, pts = meta.get("totalShares", 0), meta.get("prevTotalShares", 0)
            if pts and ts and abs(ts - pts) / pts >= 0.03:
                fund_flows.append((code, (ts - pts) / pts * 100))

        for h in holdings:
            ds = h.get("diffShares", 0)
            if ds == 0:
                continue
            amt = _best_amount(h, meta)
            wt_up = h.get("todayWeight", 0) > h.get("yestWeight", 0)
            wt_dn = h.get("todayWeight", 0) < h.get("yestWeight", 0)
            s = stock[h["code"]]
            s["name"] = h["name"]; s["code"] = h["code"]
            if ds > 0:
                s["add_amt"] += amt; s["add_etfs"].append(code); total_buy += amt
                if wt_up:
                    s["add_wt_up"] += 1
                if h.get("prevShares", 0) == 0:
                    new_pos.append((h["code"], h["name"], code, amt))
            else:
                s["red_amt"] += amt; s["red_etfs"].append(code); total_sell += amt
                if wt_dn:
                    s["red_wt_dn"] += 1
                # 全數出清(0股) 或 砍到剩 ≤1000 股(≤1張)＝實質清倉；記剩餘股數以區分措辭
                curr = h.get("shares", 0)
                if curr <= 1000:
                    cleared.append((h["code"], h["name"], code, amt, curr))

    for c, s in stock.items():
        s["net"] = s["add_amt"] + s["red_amt"]

    # 與前一交易日比較共識「加碼家數」
    rising, cooling = [], []
    if prev_snap:
        y_add = defaultdict(list)
        name_of = {}
        for etf_id, blk in prev_snap.items():
            for h in blk.get("holdings", []):
                name_of.setdefault(h["code"], h.get("name", h["code"]))
                if h.get("diffShares", 0) > 0:
                    y_add[h["code"]].append(etf_id)
        for c, s in stock.items():
            if s["name"]:
                name_of[c] = s["name"]
        for c in set(stock) | set(y_add):
            t_codes = sorted(stock[c]["add_etfs"]) if c in stock else []
            y_codes = sorted(y_add.get(c, []))
            t = len(t_codes)
            y = len(y_codes)
            nm = name_of.get(c, c)
            if t >= 2 and t > y:
                rising.append((c, nm, y, t, t_codes))     # 升溫：附今日加碼的 ETF
            elif y >= 2 and t < y:
                cooling.append((c, nm, y, t, y_codes))     # 退潮：附前一日加碼的 ETF
        rising.sort(key=lambda x: -(x[3] - x[2]))
        cooling.sort(key=lambda x: (x[3] - x[2]))

    # ---- 組訊息 ----
    md = today_str[5:].replace("-", "/").lstrip("0").replace("/0", "/")
    if total_tracked is None:
        total_tracked = len(updated)
    lines = [PROVENANCE,
             "",
             title_tmpl.format(md=md),
             subhead_tmpl.format(u=len(updated), t=total_tracked)]
    if first_day:
        lines.append(f"（{'、'.join(first_day)} 為首日資料，不列入統計）")
    lines.append("")

    # 廣度：被 ≥3 家同向加碼/減碼的檔數
    breadth_add = sum(1 for s in stock.values() if len(s["add_etfs"]) >= 3)
    breadth_red = sum(1 for s in stock.values() if len(s["red_etfs"]) >= 3)

    # 1. 多空總結（+ 廣度 + 規模變動提醒）
    net_all = total_buy + total_sell
    if total_buy or total_sell:
        bias = "整體偏多" if net_all > 0 else ("整體偏空" if net_all < 0 else "多空相當")
        n_real = len(updated) - len(first_day)
        lines.append(f"🧭 今日總結：{n_real} 檔合計買超 {yi(total_buy)}億、賣超 {yi(-total_sell)}億，"
                     f"淨{yi_signed(net_all)}億，{bias}。")
        lines.append(f"　廣度：{breadth_add} 檔被 ≥3 家同向加碼、{breadth_red} 檔被 ≥3 家同向減碼。")
        if fund_flows:
            ff = "、".join(f"{c}{'申購' if p > 0 else '贖回'}{abs(p):.0f}%" for c, p in
                          sorted(fund_flows, key=lambda x: -abs(x[1]))[:4])
            lines.append(f"　⚙️ 規模顯著變動（持股增減部分為被動、非選股）：{ff}")
        lines.append("")

    # 每檔的權重訊號：主動加碼=股數↑且權重↑；股數↑權重未升則偏被動（規模驅動）
    def _wt_note(s, side):
        if side == "add":
            n_up, n = s["add_wt_up"], len(s["add_etfs"])
            return f"，{n_up}/{n} 家提高權重" if n_up else "，皆未提高權重(恐規模驅動)"
        n_dn, n = s["red_wt_dn"], len(s["red_etfs"])
        return f"，{n_dn}/{n} 家降低權重" if n_dn else "，皆未降低權重"

    # 2a. 淨買超最多
    net_buys = sorted([s for s in stock.values() if s["net"] > 0], key=lambda x: -x["net"])[:6]
    if net_buys:
        lines.append("💰 今天淨買超最多：")
        for i, s in enumerate(net_buys, 1):
            add_codes = "、".join(sorted(s["add_etfs"]))
            tail = f"／{'、'.join(sorted(s['red_etfs']))} 減碼" if s["red_etfs"] else ""
            lines.append(f"{i}. {s['name']} {s['code']}（{add_codes} 加碼{tail}）淨 {yi_signed(s['net'])}億"
                         f"{_wt_note(s, 'add')}")
        lines.append("")

    # 有志一同（≥3 家加碼）
    consensus_add = sorted([s for s in stock.values() if len(s["add_etfs"]) >= 3],
                           key=lambda x: -len(x["add_etfs"]))
    if consensus_add:
        lines.append("🤝 有志一同（3 家以上一起加碼）：")
        for s in consensus_add:
            lines.append(f"- {s['name']} {s['code']}（{'、'.join(sorted(s['add_etfs']))}）")
        lines.append("")

    # 集體調節（≥3 家減碼）
    consensus_red = sorted([s for s in stock.values() if len(s["red_etfs"]) >= 3],
                           key=lambda x: -len(x["red_etfs"]))
    if consensus_red:
        lines.append("⚠️ 集體調節（3 家以上一起減碼）：")
        for s in consensus_red:
            lines.append(f"- {s['name']} {s['code']}（{'、'.join(sorted(s['red_etfs']))}）")
    else:
        lines.append("✅ 沒有任何一檔被 3 家以上同時砍，無集體出逃。")
    lines.append("")

    # 2b. 淨賣超最多
    net_sells = sorted([s for s in stock.values() if s["net"] < 0], key=lambda x: x["net"])[:6]
    if net_sells:
        lines.append("🔻 今天淨賣超最多：")
        for i, s in enumerate(net_sells, 1):
            red_codes = "、".join(sorted(s["red_etfs"]))
            tail = f"／{'、'.join(sorted(s['add_etfs']))} 加碼" if s["add_etfs"] else ""
            lines.append(f"{i}. {s['name']} {s['code']}（{red_codes} 減碼{tail}）淨 {yi_signed(s['net'])}億"
                         f"{_wt_note(s, 'red')}")
        lines.append("")

    # 2c. 經理人分歧（同時有加碼與減碼，至少一邊 ≥1、合計 ≥3 家）
    # 排序以「對立程度」為主：弱勢那一邊的家數 min(加,減) 越大代表越勢均力敵、
    # 越是真正的分歧（如 3加vs4減 勝過 5加vs2減 的一面倒）；同分再看總家數。
    divergence = [s for s in stock.values()
                  if s["add_etfs"] and s["red_etfs"]
                  and (len(s["add_etfs"]) + len(s["red_etfs"])) >= 3]
    divergence.sort(key=lambda x: (
        -min(len(x["add_etfs"]), len(x["red_etfs"])),
        -(len(x["add_etfs"]) + len(x["red_etfs"])),
    ))
    if divergence:
        lines.append("⚔️ 經理人分歧最大：")
        for s in divergence[:6]:
            lines.append(f"- {s['name']} {s['code']}（加碼：{'、'.join(sorted(s['add_etfs']))}｜"
                         f"減碼：{'、'.join(sorted(s['red_etfs']))}，淨 {yi_signed(s['net'])}億）")
        lines.append("")

    if rising or cooling:
        lines.append("📈 共識升溫／退潮：")
        for c, nm, y, t, codes in rising[:4]:
            lines.append(f" 🔥 {nm} {c}　{y}→{t} 家（{'、'.join(codes)} 加碼）")
        for c, nm, y, t, codes in cooling[:4]:
            lines.append(f" ❄️ {nm} {c}　{y}→{t} 家（原 {'、'.join(codes)} 加碼）")
        lines.append("")

    # 產業流向：依產業彙總淨額（排除「其他」未分類）
    sector_net = defaultdict(float)
    for s in stock.values():
        sec = _sector(s["code"])
        if sec != "其他":
            sector_net[sec] += s["net"]
    sec_buy = sorted([(k, v) for k, v in sector_net.items() if v > 0], key=lambda x: -x[1])[:3]
    sec_sell = sorted([(k, v) for k, v in sector_net.items() if v < 0], key=lambda x: x[1])[:3]
    if sec_buy or sec_sell:
        lines.append("🏭 產業資金流向（淨額）：")
        if sec_buy:
            lines.append("　流入： " + "、".join(f"{k} {yi_signed(v)}億" for k, v in sec_buy))
        if sec_sell:
            lines.append("　流出： " + "、".join(f"{k} {yi_signed(v)}億" for k, v in sec_sell))
        lines.append("")

    big_new = sorted([x for x in new_pos if abs(x[3]) >= 1e7], key=lambda x: -x[3])[:6]
    if big_new:
        lines.append("🆕 新建倉： " + "、".join(f"{n} {c}（{e}）" for c, n, e, _ in big_new))
    big_clear = sorted([x for x in cleared if abs(x[3]) >= 1e7], key=lambda x: x[3])[:6]
    if big_clear:
        def _clear_txt(c, n, e, da, rem):
            tail = "全數出清" if rem == 0 else f"減至剩 {rem/1000:g} 張"
            return f"{n} {c}（{e} {tail} {yi(da)}億）"
        lines.append("🗑️ 被清倉／砍到剩零股： " + "、".join(_clear_txt(*x) for x in big_clear))
    if big_new or big_clear:
        lines.append("")

    # ---- 規則式「今日觀察」----
    recent = _recent_snapshot_dirs(today_str)
    obs = []
    # 多空力道
    if total_buy or total_sell:
        ratio = total_buy / -total_sell if total_sell < 0 else 0
        if net_all > 0 and ratio >= 1.5:
            obs.append(f"- 買盤明顯佔優：買超是賣超的約 {ratio:.1f} 倍，經理人整體加碼意願強。")
        elif net_all < 0 and 0 < ratio <= 0.67:
            obs.append(f"- 賣壓明顯佔優：賣超是買超的約 {(1/ratio):.1f} 倍，經理人整體偏防禦。")
    # 集中度（用淨買超龍頭）
    if net_buys and total_buy > 0:
        top = net_buys[0]
        share = top["add_amt"] / total_buy * 100
        add_codes = "、".join(sorted(top["add_etfs"]))
        n_add = len(top["add_etfs"])
        if n_add == 1:
            obs.append(f"- {top['name']} {top['code']} 僅由 {add_codes} 單一 ETF 貢獻最大淨買超（{yi_signed(top['net'])}億），"
                       f"屬個別經理人觀點、非全市場共識。")
        elif share >= 40:
            obs.append(f"- {top['name']} {top['code']} 一檔獨大——{add_codes} 共 {n_add} 家加碼，佔今日買超約 {share:.0f}%。")
    # 連續走勢（streak）
    if net_buys:
        st, cum = _streak_cum(recent, net_buys[0]["code"], "add")
        if st >= 2:
            obs.append(f"- {net_buys[0]['name']} {net_buys[0]['code']} 已連 {st} 日獲加碼，"
                       f"期間累計淨 {yi_signed(cum)}億，買盤具延續性。")
    if net_sells:
        st, cum = _streak_cum(recent, net_sells[0]["code"], "red")
        if st >= 2:
            obs.append(f"- {net_sells[0]['name']} {net_sells[0]['code']} 已連 {st} 日被調節，"
                       f"期間累計淨 {yi_signed(cum)}億，賣壓持續。")
    # 分歧
    if divergence:
        s = divergence[0]
        obs.append(f"- 分歧最大：{s['name']} {s['code']} 有 {len(s['add_etfs'])} 家加碼、{len(s['red_etfs'])} 家減碼，"
                   f"經理人看法分歧。")
    # 買盤共識熄火（前一日 ≥3 家加碼、今日歸零；措辭依是否真有減碼分流，避免誤讀為大幅砍倉）
    sharp_cool = [x for x in cooling if x[2] >= 3 and x[3] == 0]
    if sharp_cool:
        names = "、".join(f"{nm} {c}（{y}→0 家）" for c, nm, y, t, _codes in sharp_cool[:3])
        sold = any(c in stock and stock[c]["red_etfs"] for c, nm, y, t, _codes in sharp_cool[:3])
        if sold:
            obs.append(f"- 買盤熄火且現賣壓：{names}——前一日多家共識買，今日無人續買、部分轉為減碼，注意獲利了結。")
        else:
            obs.append(f"- 買盤暫歇：{names}——前一日多家共識買，今日無人續加碼（持股未減、僅停止買進），觀察是否轉為調節。")
    if big_clear:
        c, n, e, da, rem = big_clear[0]
        act = "全數出清" if rem == 0 else f"砍到剩 {rem/1000:g} 張"
        obs.append(f"- 最大清倉：{n} {c} 被 {e} {act} {yi(da)}億。")
    if rising:
        c, nm, y, t, codes = rising[0]
        obs.append(f"- 新共識成形：{nm} {c} 加碼家數 {y}→{t}（{'、'.join(codes)}），資金開始聚集。")
    if obs:
        lines.append("💡 今日觀察")
        lines.extend(obs)
        lines.append("")

    return "\n".join(lines), len(updated)


def save_digest(date_str, message, path=DIGESTS_FILE):
    """把分析文字寫入指定 digests 檔（{date: text}），供網站讀取。重跑以最新版覆蓋。"""
    data = {}
    if os.path.exists(path):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            data = {}
    data[date_str] = message
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    log.info(f"Saved digest text to {path} ({date_str}).")


def _read_marker(path):
    """marker 內容格式為 'YYYY-MM-DD count'；回傳 (date, count)。"""
    if not os.path.exists(path):
        return None, 0
    parts = open(path, encoding="utf-8").read().strip().split()
    date = parts[0] if parts else None
    cnt = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return date, cnt


def run_group(group, now):
    """
    為單一群組產生 + 存檔 + 發送。第一份達門檻即發；之後同一報告日若「已更新檔數
    增加」會再發一份更新版（marker 記錄 '報告日 已發送檔數'），無新增則不重發。
    """
    g = GROUPS[group]
    today_str = now.strftime("%Y-%m-%d")
    report_date, message, count = build_digest(today_str, group=group)
    threshold = g["threshold_std"] if now.hour < g["fallback_hour"] else g["threshold_fallback"]

    # 資料夠完整才存檔給網站（重跑覆蓋成最新版）
    if count >= threshold:
        save_digest(report_date, message, path=g["digests_file"])

    marker = g["marker"]
    m_date, m_count = _read_marker(marker)

    if count < threshold:
        log.info(f"[{group}] only {count} ETFs for {report_date} (need {threshold}). Waiting.")
        return
    # 同一報告日且檔數沒增加 → 不重發
    if m_date == report_date and count <= m_count:
        log.info(f"[{group}] {report_date} already sent at {m_count} 檔, no new update ({count}). Skip.")
        return

    label = "更新版" if (m_date == report_date and m_count > 0) else "第一份"
    if send_telegram(message + "\n\n" + SITE_URL):
        with open(marker, "w", encoding="utf-8") as f:
            f.write(f"{report_date} {count}")
        log.info(f"[{group}] {label} sent ({report_date}, {count} 檔), marker updated.")
    else:
        log.warning(f"[{group}] send failed; marker not written (will retry next run).")


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    for group in GROUPS:
        try:
            run_group(group, now)
        except Exception as e:
            log.warning(f"[{group}] digest failed: {e}")


if __name__ == "__main__":
    main()
