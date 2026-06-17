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

MARKER_FILE = "last_digest.txt"
DIGESTS_FILE = "digests.json"   # 供網站「每日分析」分頁讀取（{date: text}）
SITE_URL = "https://wuminwu.github.io/woody-etf-tracker/"
SEND_THRESHOLD = 10   # 12 檔台股 ETF 中至少 10 檔更新才發送
FALLBACK_HOUR = 21    # TW 21:00 後放寬條件
FALLBACK_MIN_COUNT = 6

# 台股 ETF（不含 00988A 海外）
TW_ETFS = [
    ("00981A", "統一台股增長"),
    ("00400A", "國泰動能高息"),
    ("00403A", "統一升級50"),
    ("00404A", "聯博動能50"),
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


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set — printing message instead.")
        print(message)
        return False
    try:
        payload = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as e:
        log.warning(f"Telegram failed: {e}")
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


def _gather_from_live(date_str):
    """從 live data_{code}.json 蒐集當日各 ETF 的 holdings + meta。"""
    etf_data, updated = {}, []
    for code, _name in TW_ETFS:
        path = f"data_{code}.json"
        if not os.path.exists(path):
            continue
        d = json.loads(open(path, encoding="utf-8").read())
        if d["meta"].get("dataDate") != date_str:
            continue
        updated.append(code)
        etf_data[code] = {"holdings": d.get("holdings", []), "meta": d.get("meta", {})}
    return etf_data, updated


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
    """回傳最近 n 個（含 ref_date）snapshot 的 (date, 加碼股票set, 減碼股票set)，升冪。"""
    files = sorted(glob.glob("snapshots/????-??-??.json"))
    files = [f for f in files if os.path.basename(f)[:10] <= ref_date_str][-n:]
    out = []
    for f in files:
        try:
            snap = json.loads(open(f, encoding="utf-8").read())
        except Exception:
            continue
        adds, reds = set(), set()
        for _etf, blk in snap.items():
            for h in blk.get("holdings", []):
                ds = h.get("diffShares", 0)
                if ds > 0:
                    adds.add(h["code"])
                elif ds < 0:
                    reds.add(h["code"])
        out.append((os.path.basename(f)[:10], adds, reds))
    return out


def _streak(recent, code, want):
    """從最近日往回算 code 連續被 want('add'/'red') 的天數。"""
    cnt = 0
    for _date, adds, reds in reversed(recent):
        s = adds if want == "add" else reds
        if code in s:
            cnt += 1
        else:
            break
    return cnt


def yi_signed(amount):
    """元 → 億，帶正負號，一位小數。"""
    v = amount / 1e8
    return f"{'+' if v >= 0 else ''}{v:.1f}"


def build_digest(today_str):
    """今日版：從 live data_*.json 蒐集，與前一日 snapshot 比較。"""
    etf_data, updated = _gather_from_live(today_str)
    prev_snap = find_prev_snapshot(today_str)
    return render_digest(today_str, etf_data, updated, prev_snap)


def render_digest(today_str, etf_data, updated, prev_snap):
    """
    依當日各 ETF 的 holdings + meta 與前一日 snapshot 產生分析文字。
    etf_data: { code: {"holdings": [...], "meta": {...}} }
    updated:  有當日資料的 ETF 代號清單（含首日 ETF）
    prev_snap: 前一交易日 snapshot dict 或 None
    回傳 (message, updated_count)。message 不含結尾網站連結（由發送端另加）。
    """
    first_day = []
    # 個股淨額聚合：code -> {name, add_amt, red_amt(負), add_etfs[], red_etfs[]}
    stock = defaultdict(lambda: {"name": "", "add_amt": 0.0, "red_amt": 0.0,
                                 "add_etfs": [], "red_etfs": []})
    new_pos, cleared = [], []
    total_buy, total_sell = 0.0, 0.0   # 全市場毛買超 / 毛賣超（賣為負）

    for code in updated:
        holdings = etf_data.get(code, {}).get("holdings", [])
        meta = etf_data.get(code, {}).get("meta", {})
        active = [h for h in holdings if h.get("shares", 0) > 0]
        if active and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8:
            first_day.append(code)
            continue

        for h in holdings:
            ds = h.get("diffShares", 0)
            if ds == 0:
                continue
            amt = _best_amount(h, meta)
            s = stock[h["code"]]
            s["name"] = h["name"]; s["code"] = h["code"]
            if ds > 0:
                s["add_amt"] += amt; s["add_etfs"].append(code); total_buy += amt
                if h.get("prevShares", 0) == 0:
                    new_pos.append((h["code"], h["name"], code, amt))
            else:
                s["red_amt"] += amt; s["red_etfs"].append(code); total_sell += amt
                if h.get("shares", 0) == 0:
                    cleared.append((h["code"], h["name"], code, amt))

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
            t = len(stock[c]["add_etfs"]) if c in stock else 0
            y = len(y_add.get(c, []))
            nm = name_of.get(c, c)
            if t >= 2 and t > y:
                rising.append((c, nm, y, t))
            elif y >= 2 and t < y:
                cooling.append((c, nm, y, t))
        rising.sort(key=lambda x: -(x[3] - x[2]))
        cooling.sort(key=lambda x: (x[3] - x[2]))

    # ---- 組訊息 ----
    md = today_str[5:].replace("-", "/").lstrip("0").replace("/0", "/")
    total_tracked = len(TW_ETFS)
    lines = ["報告來源: 854-Woody (未經同意請勿轉傳，若數據有誤請通知我)",
             "",
             f"📊 {md} 主動 ETF 經理人都在買什麼",
             f"（{len(updated)}/{total_tracked} 檔已更新持股；00988A 海外 T+1 不列入）"]
    if first_day:
        lines.append(f"（{'、'.join(first_day)} 為首日資料，不列入統計）")
    lines.append("")

    # 1. 多空總結
    net_all = total_buy + total_sell
    if total_buy or total_sell:
        bias = "整體偏多" if net_all > 0 else ("整體偏空" if net_all < 0 else "多空相當")
        n_real = len(updated) - len(first_day)
        lines.append(f"🧭 今日總結：{n_real} 檔合計買超 {yi(total_buy)}億、賣超 {yi(-total_sell)}億，"
                     f"淨{yi_signed(net_all)}億，{bias}。")
        lines.append("")

    # 2a. 淨買超最多
    net_buys = sorted([s for s in stock.values() if s["net"] > 0], key=lambda x: -x["net"])[:6]
    if net_buys:
        lines.append("💰 今天淨買超最多：")
        for i, s in enumerate(net_buys, 1):
            add_codes = "、".join(sorted(s["add_etfs"]))
            tail = f"／{'、'.join(sorted(s['red_etfs']))} 減碼" if s["red_etfs"] else ""
            lines.append(f"{i}. {s['name']} {s['code']}（{add_codes} 加碼{tail}）淨 {yi_signed(s['net'])}億")
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
            lines.append(f"{i}. {s['name']} {s['code']}（{red_codes} 減碼{tail}）淨 {yi_signed(s['net'])}億")
        lines.append("")

    # 2c. 經理人分歧（同時有加碼與減碼，至少一邊 ≥1、合計 ≥3 家）
    divergence = [s for s in stock.values()
                  if s["add_etfs"] and s["red_etfs"]
                  and (len(s["add_etfs"]) + len(s["red_etfs"])) >= 3]
    divergence.sort(key=lambda x: -(len(x["add_etfs"]) + len(x["red_etfs"])))
    if divergence:
        lines.append("⚔️ 經理人分歧最大：")
        for s in divergence[:4]:
            lines.append(f"- {s['name']} {s['code']}（加碼：{'、'.join(sorted(s['add_etfs']))}｜"
                         f"減碼：{'、'.join(sorted(s['red_etfs']))}，淨 {yi_signed(s['net'])}億）")
        lines.append("")

    if rising or cooling:
        lines.append("📈 共識升溫／退潮：")
        for c, nm, y, t in rising[:4]:
            lines.append(f" 🔥 {nm} {c}　{y}→{t} 家")
        for c, nm, y, t in cooling[:4]:
            lines.append(f" ❄️ {nm} {c}　{y}→{t} 家")
        lines.append("")

    big_new = sorted([x for x in new_pos if abs(x[3]) >= 1e7], key=lambda x: -x[3])[:6]
    if big_new:
        lines.append("🆕 新建倉： " + "、".join(f"{n} {c}（{e}）" for c, n, e, _ in big_new))
    big_clear = sorted([x for x in cleared if abs(x[3]) >= 1e7], key=lambda x: x[3])[:6]
    if big_clear:
        lines.append("🗑️ 被清倉： " + "、".join(f"{n} {c}（{e} 砍 {yi(da)}億）" for c, n, e, da in big_clear))
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
        st = _streak(recent, net_buys[0]["code"], "add")
        if st >= 2:
            obs.append(f"- {net_buys[0]['name']} {net_buys[0]['code']} 已連 {st} 日獲加碼，買盤具延續性。")
    if net_sells:
        st = _streak(recent, net_sells[0]["code"], "red")
        if st >= 2:
            obs.append(f"- {net_sells[0]['name']} {net_sells[0]['code']} 已連 {st} 日被調節，賣壓持續。")
    # 分歧
    if divergence:
        s = divergence[0]
        obs.append(f"- 分歧最大：{s['name']} {s['code']} 有 {len(s['add_etfs'])} 家加碼、{len(s['red_etfs'])} 家減碼，"
                   f"經理人看法分歧。")
    # 急轉直下
    sharp_cool = [x for x in cooling if x[2] >= 3 and x[3] == 0]
    if sharp_cool:
        names = "、".join(f"{nm} {c}（{y}→0 家）" for c, nm, y, t in sharp_cool[:3])
        sold = any(c in stock and stock[c]["red_etfs"] for c, nm, y, t in sharp_cool[:3])
        tail = "部分已轉為減碼，注意短線資金獲利了結。" if sold else "加碼動能消退，後續觀察是否轉為調節。"
        obs.append(f"- 急轉直下：{names}——前一日還是多家共識買，今日歸零，{tail}")
    if big_clear:
        c, n, e, da = big_clear[0]
        obs.append(f"- 最大清倉：{n} {c} 被 {e} 全數出清 {yi(da)}億。")
    if rising:
        c, nm, y, t = rising[0]
        obs.append(f"- 新共識成形：{nm} 加碼家數 {y}→{t}，資金開始聚集。")
    if obs:
        lines.append("💡 今日觀察")
        lines.extend(obs)
        lines.append("")

    return "\n".join(lines), len(updated)


def save_digest(date_str, message):
    """把當日分析文字寫入 digests.json（{date: text}），供網站讀取。重跑會以最新版覆蓋。"""
    data = {}
    if os.path.exists(DIGESTS_FILE):
        try:
            data = json.loads(open(DIGESTS_FILE, encoding="utf-8").read())
        except Exception:
            data = {}
    data[date_str] = message
    with open(DIGESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    log.info(f"Saved digest text to {DIGESTS_FILE} ({date_str}).")


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    today_str = now.strftime("%Y-%m-%d")

    message, updated_count = build_digest(today_str)
    threshold = SEND_THRESHOLD if now.hour < FALLBACK_HOUR else FALLBACK_MIN_COUNT

    # 資料夠完整才存檔給網站（避免清晨資料殘缺就覆蓋）；重跑會更新成最新版本
    if updated_count >= threshold:
        save_digest(today_str, message)

    # Telegram：當日已發送過就不重送
    if os.path.exists(MARKER_FILE) and open(MARKER_FILE, encoding="utf-8").read().strip() == today_str:
        log.info("Digest already sent today (text saved; skipping Telegram).")
        return

    if updated_count < threshold:
        log.info(f"Only {updated_count} ETFs updated (need {threshold}). Waiting for more.")
        return

    if send_telegram(message + "\n\n" + SITE_URL):
        with open(MARKER_FILE, "w", encoding="utf-8") as f:
            f.write(today_str)
        log.info(f"Digest sent and marker written ({today_str}).")
    else:
        log.warning("Digest send failed; marker not written (will retry next run).")


if __name__ == "__main__":
    main()
