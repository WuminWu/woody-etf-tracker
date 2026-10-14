"""
etf_registry.py
---------------
追蹤中的 ETF 清單唯一來源：etfs.json（網頁 app.js 也直接讀它）。
新增一檔 ETF＝在 etfs.json 加一筆＋放上 check_and_update_{代號}.py，其他地方不用改。

etfs.json 每筆欄位：
  code        代號
  name        網頁排行／比較／共同加減碼用的名稱
  fullName    網頁下拉選單的完整名稱
  shortName   每日／每週摘要用的短名
  group       "tw"（台股，當日揭露）或 "overseas"（海外/混合，摘要另成一組）
  t1          網頁是否加註「海外ETF晚1日」（持股日＝前一交易日）
  ipo         [掛牌日, 發行價] 或 null；掛牌當年 YTD 以發行價為基準
  dist        配息 {freq, months, day, note}；months 空陣列＝不顯示下次配息日
  risk        風險收益等級（RR1~RR5）；riskVerified=false 表示沿用預設 RR4、尚未對過官網
陣列順序＝本機排程執行順序。
"""

import json
import os
from urllib.parse import urlencode

REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etfs.json")

# 網站網址（全專案單一來源）。原本 21 支爬蟲、日報、週報、看門狗都寫死
# https://wuminwu.github.io/woody-etf-tracker/ ，但 GitHub Pages 早已停用（回 404），網站已搬到 Cloudflare Pages。
SITE_URL = "https://woody-etf-tracker.pages.dev/"


def site_link(etf=None, prefix="🔗 ", **params):
    """網站連結。帶 etf 或其他參數時加上 #etf=…&tab=…（網頁目前尚未讀取 hash，先一律開到首頁）。

    site_link("00981A")                                   → 🔗 …/#etf=00981A
    site_link(tab="digest", dtype="tw_daily", ddate="2026-10-13", prefix="")
    """
    q = {"etf": etf} if etf else {}
    q.update({k: v for k, v in params.items() if v})
    return prefix + SITE_URL + ("#" + urlencode(q) if q else "")


def load():
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        return json.load(f)


def get(code):
    return next((e for e in load() if e["code"] == code), None)


def codes(group=None):
    return [e["code"] for e in load() if group is None or e["group"] == group]


def pairs(field="name", group=None):
    """[(code, 名稱), ...]，給原本寫死 (代號, 名稱) 清單的地方用。"""
    return [(e["code"], e[field]) for e in load() if group is None or e["group"] == group]


def ipo_baseline():
    """{code: (掛牌日, 發行價)}，只含有填 ipo 的基金。"""
    return {e["code"]: tuple(e["ipo"]) for e in load() if e.get("ipo")}


if __name__ == "__main__":
    # run_update.ps1 / daily_update.yml 用：印出排程要跑的爬蟲（每行一支）
    import sys
    skip = set(sys.argv[1:])
    for c in codes():
        if c not in skip:
            print(f"check_and_update_{c}.py")
