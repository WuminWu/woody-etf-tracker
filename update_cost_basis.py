"""
update_cost_basis.py
--------------------
維護「每檔 ETF × 每檔個股」的持股成本價（平均成本法），寫入 cost_basis.json 供前端顯示。

規則：
  1. 新建倉（prevShares=0 → shares>0）：以當日收盤價記為成本
  2. 加碼（diffShares>0 且已有紀錄）：加權平均重算
       新成本 = (舊成本×舊股數 + 當日收盤×加碼股數) / 新股數
  3. 減碼不改成本（平均成本法），僅同步股數；出清（shares=0）→ 刪除紀錄，
     日後重新買回從新起算
  4. 功能上線前就持有、無法回推起始成本的 → 無紀錄（前端顯示「無紀錄」）；
     這類個股之後再加碼也維持無紀錄（總均價不可知），直到出清後重新建倉
  5. 首日 ETF 偵測（>80% 持股 prevShares=0 = 剛納入追蹤）：整組合跳過，
     避免把「開始追蹤」誤記為「全部新建倉」

冪等：cost_basis.json 的 _lastProcessed 記錄各 ETF 已處理到的 dataDate，
排程每 30 分鐘重跑也不會重複計算。

檔案結構：
{
  "_lastProcessed": { "00981A": "2026-07-07", ... },
  "00981A": { "2330": {"avgCost": 1050.0, "shares": 135000, "since": "2026-05-02"}, ... }
}
"""

import glob
import json
import os
import logging
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

COST_FILE = "cost_basis.json"


def load_cost_basis():
    if os.path.exists(COST_FILE):
        try:
            return json.loads(open(COST_FILE, encoding="utf-8").read())
        except Exception:
            pass
    return {"_lastProcessed": {}}


def save_cost_basis(cb):
    with open(COST_FILE, "w", encoding="utf-8") as f:
        json.dump(cb, f, ensure_ascii=False, indent=1)


def _est_price(h, meta):
    """該股當日單價（元/股）：收盤價 → diffAmount/diffShares → 權重×淨資產回推。"""
    p = h.get("price", 0) or 0
    if p > 0:
        return p
    ds, da = h.get("diffShares", 0), h.get("diffAmount", 0) or 0
    if ds and da:
        p = abs(da / ds)
        if p > 0:
            return p
    aum = (meta.get("totalMarketCap") or 0) * 1e8
    if h.get("shares", 0) > 0 and h.get("todayWeight", 0) > 0 and aum > 0:
        return (h["todayWeight"] / 100) * aum / h["shares"]
    return 0.0


def is_first_day(holdings):
    active = [h for h in holdings if h.get("shares", 0) > 0]
    return bool(active) and sum(1 for h in active if h.get("prevShares", 0) == 0) / len(active) > 0.8


def apply_day(cb, etf, holdings, meta, date_str):
    """把某 ETF 某一天的持股異動套用到 cost_basis。回傳異動筆數。"""
    book = cb.setdefault(etf, {})
    changed = 0

    if is_first_day(holdings):
        log.info(f"  [{etf}] {date_str} 為首日資料（剛納入追蹤），跳過建倉記錄")
        return 0

    seen = set()
    for h in holdings:
        code = str(h.get("code", ""))
        if not code:
            continue
        seen.add(code)
        shares = h.get("shares", 0)
        prev = h.get("prevShares", 0)
        ds = h.get("diffShares", 0)
        rec = book.get(code)

        if shares <= 0:
            # 出清 → 刪除紀錄，之後重新買回從新起算
            if rec:
                del book[code]
                changed += 1
            continue

        if prev == 0 and shares > 0:
            # 新建倉：當日收盤價為成本
            price = _est_price(h, meta)
            if price > 0:
                book[code] = {"avgCost": round(price, 2), "shares": shares, "since": date_str}
                changed += 1
            continue

        if rec is None:
            continue   # 無紀錄的舊持股：加碼也不追蹤（總均價不可知）

        if ds > 0:
            price = _est_price(h, meta)
            if price > 0:
                old_shares = rec.get("shares", prev) or prev
                new_avg = (rec["avgCost"] * old_shares + price * ds) / (old_shares + ds)
                rec["avgCost"] = round(new_avg, 2)
            rec["shares"] = shares
            changed += 1
        elif ds < 0:
            rec["shares"] = shares   # 減碼：成本不變，同步股數
            changed += 1
        else:
            rec["shares"] = shares

    # 已有紀錄但今日持股清單完全沒出現 → 視為已出清
    for code in [c for c in book if c not in seen]:
        del book[code]
        changed += 1
    return changed


def main():
    cb = load_cost_basis()
    last = cb.setdefault("_lastProcessed", {})
    total = 0
    for path in sorted(glob.glob("data_0*.json")):
        etf = os.path.basename(path)[5:-5]
        try:
            d = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        meta = d.get("meta", {})
        date_str = meta.get("dataDate", "")
        if not date_str or last.get(etf, "") >= date_str:
            continue   # 已處理過這天 → 冪等跳過
        n = apply_day(cb, etf, d.get("holdings", []), meta, date_str)
        last[etf] = date_str
        total += n
        log.info(f"  [{etf}] {date_str} 處理完成，異動 {n} 筆，追蹤 {len(cb.get(etf, {}))} 檔")
    save_cost_basis(cb)
    log.info(f"cost_basis.json 已更新（本次異動 {total} 筆）")


if __name__ == "__main__":
    main()
