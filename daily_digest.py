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
    """從 live data_{code}.json 蒐集當日各 ETF 的 holdings。"""
    etf_holdings, updated = {}, []
    for code, _name in TW_ETFS:
        path = f"data_{code}.json"
        if not os.path.exists(path):
            continue
        d = json.loads(open(path, encoding="utf-8").read())
        if d["meta"].get("dataDate") != date_str:
            continue
        updated.append(code)
        etf_holdings[code] = d.get("holdings", [])
    return etf_holdings, updated


def _gather_from_snapshot(snap):
    """從 snapshot dict（{etf:{holdings}}）蒐集當日各 TW ETF 的 holdings。"""
    tw_codes = {c for c, _ in TW_ETFS}
    etf_holdings, updated = {}, []
    for etf_id, blk in snap.items():
        if etf_id not in tw_codes:
            continue
        updated.append(etf_id)
        etf_holdings[etf_id] = blk.get("holdings", [])
    return etf_holdings, updated


def build_digest(today_str):
    """今日版：從 live data_*.json 蒐集，與前一日 snapshot 比較。"""
    etf_holdings, updated = _gather_from_live(today_str)
    prev_snap = find_prev_snapshot(today_str)
    return render_digest(today_str, etf_holdings, updated, prev_snap)


def render_digest(today_str, etf_holdings, updated, prev_snap):
    """
    依當日各 ETF 的 holdings 與前一日 snapshot 產生分析文字。
    etf_holdings: { code: [holding, ...] }；holding 需含 code/name/shares/prevShares/diffShares/diffAmount
    updated:      有當日資料的 ETF 代號清單（含首日 ETF）
    prev_snap:    前一交易日 snapshot dict（{etf:{holdings}}）或 None
    回傳 (message, updated_count)。message 不含結尾網站連結（由發送端另加）。
    """
    first_day = []
    add_map = defaultdict(lambda: {"name": "", "amt": 0.0, "etfs": []})
    red_map = defaultdict(lambda: {"name": "", "amt": 0.0, "etfs": []})
    new_pos, cleared = [], []

    for code in updated:
        holdings = etf_holdings.get(code, [])
        active = [h for h in holdings if h.get("shares", 0) > 0]
        # 首日 ETF 偵測：絕大多數持股 prevShares=0 → 全組合視為新增，屬雜訊
        if active and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8:
            first_day.append(code)
            continue

        for h in holdings:
            ds = h.get("diffShares", 0)
            da = h.get("diffAmount", 0) or 0
            prev = h.get("prevShares", 0)
            curr = h.get("shares", 0)
            if ds > 0:
                m = add_map[h["code"]]
                m["name"] = h["name"]; m["amt"] += da; m["etfs"].append(code)
                if prev == 0:
                    new_pos.append((h["code"], h["name"], code, da))
            elif ds < 0:
                m = red_map[h["code"]]
                m["name"] = h["name"]; m["amt"] += da; m["etfs"].append(code)
                if curr == 0:
                    cleared.append((h["code"], h["name"], code, da))

    # 與前一交易日比較共識家數
    rising, cooling = [], []
    if prev_snap:
        y_add = defaultdict(list)
        name_of = {}
        for etf_id, blk in prev_snap.items():
            for h in blk.get("holdings", []):
                name_of.setdefault(h["code"], h.get("name", h["code"]))
                if h.get("diffShares", 0) > 0:
                    y_add[h["code"]].append(etf_id)
        for c, m in list(add_map.items()) + list(red_map.items()):
            if m["name"]:
                name_of[c] = m["name"]
        for c in set(add_map) | set(y_add):
            t = len(add_map[c]["etfs"]) if c in add_map else 0
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
    lines = [f"📊 {md} 主動 ETF 經理人都在買什麼",
             f"（{len(updated)}/{total_tracked} 檔已更新持股；00988A 海外 T+1 不列入）"]
    if first_day:
        lines.append(f"（{'、'.join(first_day)} 為首日資料，不列入統計）")
    lines.append("")

    top_buys = sorted(add_map.items(), key=lambda x: -x[1]["amt"])[:6]
    if top_buys:
        lines.append("💰 今天被買最兇的幾檔：")
        for i, (c, m) in enumerate(top_buys, 1):
            etfs = "、".join(sorted(m["etfs"]))
            lines.append(f"{i}. {m['name']} {c} 約 +{yi(m['amt'])}億（{etfs}）")
        lines.append("")

    consensus_add = [(c, m) for c, m in add_map.items() if len(m["etfs"]) >= 3]
    consensus_add.sort(key=lambda x: -len(x[1]["etfs"]))
    if consensus_add:
        lines.append("🤝 有志一同（3 家以上一起加碼）：")
        for c, m in consensus_add:
            lines.append(f"- {m['name']} {c}（{'、'.join(sorted(m['etfs']))}）")
        lines.append("")

    consensus_red = [(c, m) for c, m in red_map.items() if len(m["etfs"]) >= 3]
    consensus_red.sort(key=lambda x: -len(x[1]["etfs"]))
    if consensus_red:
        lines.append("⚠️ 集體調節（3 家以上一起減碼）：")
        for c, m in consensus_red:
            lines.append(f"- {m['name']} {c}（{'、'.join(sorted(m['etfs']))}）")
    else:
        lines.append("✅ 沒有任何一檔被 3 家以上同時砍，無集體出逃。")
    lines.append("")

    top_sells = sorted(red_map.items(), key=lambda x: x[1]["amt"])[:6]
    if top_sells:
        lines.append("🔻 今天被調節最重的幾檔：")
        for i, (c, m) in enumerate(top_sells, 1):
            etfs = "、".join(sorted(m["etfs"]))
            lines.append(f"{i}. {m['name']} {c} 約 {yi(m['amt'])}億（{etfs}）")
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
    obs = []
    if top_buys:
        total_buy = sum(m["amt"] for _, m in add_map.items())
        c0, m0 = top_buys[0]
        share = m0["amt"] / total_buy * 100 if total_buy > 0 else 0
        n_etf = len(m0["etfs"])
        if share >= 40:
            obs.append(f"- {m0['name']}一檔獨大——{n_etf} 家共買 +{yi(m0['amt'])}億，"
                       f"佔今日全部買超金額約 {share:.0f}%。")
        else:
            obs.append(f"- 今日買超最集中在{m0['name']}（+{yi(m0['amt'])}億、{n_etf} 家），"
                       f"佔整體買超約 {share:.0f}%。")
    sharp_cool = [x for x in cooling if x[2] >= 3 and x[3] == 0]
    if sharp_cool:
        names = "、".join(f"{nm}（{y}→0 家）" for _, nm, y, t in sharp_cool[:3])
        # 只有當這些股票今日確實出現在 red_map（有 ETF 減碼）時，才用「獲利了結」措辭；
        # 否則 4→0 可能只是停止加碼、持股不動，措辭改為中性的「加碼動能消退」。
        sold = any(c in red_map for c, nm, y, t in sharp_cool[:3])
        tail = "部分已轉為減碼，注意短線資金獲利了結。" if sold else "加碼動能消退，後續觀察是否轉為調節。"
        obs.append(f"- 急轉直下：{names}——前一日還是多家共識買，今日歸零，{tail}")
    if big_clear:
        c, n, e, da = big_clear[0]
        obs.append(f"- 最大清倉：{n} 被 {e} 全數出清 {yi(da)}億。")
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
