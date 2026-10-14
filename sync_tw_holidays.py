# -*- coding: utf-8 -*-
"""
sync_tw_holidays.py — 從證交所匯入「當年＋次年」的台股休市日 → tw_holidays_twse.json。

為什麼：休市日表原本只有手寫的 2026 年。跨年後元旦、春節會被當成交易日——
T+1 爬蟲等不到休市日的資料、群益（輸入 T+1）會撞錯誤對話框當掉，
只保留最新一天的來源漏抓就補不回來（2026 年 4/30、6/18、7/9 就是這樣遺失的）。
證交所一公布次年行事曆，這支就會自動匯入，tw_calendar 立即生效。

排程：run_update.ps1 每輪第一步（在 git pull 之後，產生的檔案同一輪就提交）；
      每天只真的連線一次。手動強制：python sync_tw_holidays.py --force

規則（以 2026 年驗證：與手寫清單完全一致，只差臨時颱風假 7/10）：
  證交所清單中「名稱不以『交易日』結尾」且落在週一～五的日子＝休市
  （排除「國曆新年開始交易日」「農曆春節前最後交易日」這類提示；
    「市場無交易，僅辦理結算交割作業」＝封關，算休市）。
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date={year}0101&response=json"
OUT = "tw_holidays_twse.json"
MARKER = os.path.join("logs", "tw_holidays_sync.txt")


def parse(doc):
    """證交所回應 → {date: 名稱}；尚未公布（data 為空）回 None。"""
    rows = doc.get("data") or []
    if not rows:
        return None
    out = {}
    for row in rows:
        ds, name = row[0], str(row[1]).strip()
        d = date.fromisoformat(ds)
        if d.weekday() >= 5 or name.endswith("交易日"):
            continue
        if name.startswith("市場無交易"):
            name = "封關（無交易，僅結算交割）"
        out[d] = name
    return out


def fetch(year):
    req = urllib.request.Request(URL.format(year=year), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return parse(json.loads(r.read().decode("utf-8")))


def load_state():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"years": [], "holidays": {}}


def save_state(state):
    state["holidays"] = dict(sorted(state["holidays"].items()))
    state["years"] = sorted(set(state["years"]))
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, OUT)


def main(argv):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if "--force" not in argv:
        try:
            if open(MARKER, encoding="utf-8").read().strip() == today.isoformat():
                print("今天已同步過，略過。")
                return 0
        except FileNotFoundError:
            pass

    state = load_state()
    added, removed, new_years, unpublished = [], [], [], []
    for year in (today.year, today.year + 1):
        try:
            got = fetch(year)
        except Exception as e:
            print(f"{year}：證交所連線失敗（{e}），保留現有資料")
            continue
        if got is None:
            unpublished.append(year)
            continue
        old = {k: v for k, v in state["holidays"].items() if k.startswith(f"{year}-")}
        new = {d.isoformat(): n for d, n in got.items()}
        added += [(k, n) for k, n in sorted(new.items()) if k not in old]
        removed += [k for k in sorted(old) if k not in new]
        for k in old:
            del state["holidays"][k]
        state["holidays"].update(new)
        if year not in state["years"]:
            new_years.append(year)
            state["years"].append(year)

    changed = bool(added or removed or new_years)
    if changed:
        save_state(state)
        # 同一輪重新匯出 holidays.json（run_update.ps1 的休市守門讀它），兩個檔一起提交；
        # 否則下一輪開頭重新匯出時，工作目錄有未提交的修改會讓 git pull 失敗
        subprocess.run([sys.executable, "tw_calendar.py"], check=False)
    os.makedirs("logs", exist_ok=True)
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write(today.isoformat())

    for y in unpublished:
        print(f"{y}：證交所尚未公布休市日")
    if not changed:
        print(f"休市日已是最新（已匯入年份：{state['years']}）")
        return 0
    lines = [f"📅 已自動從證交所匯入台股休市日（{', '.join(map(str, sorted(set(state['years']))))} 年）"]
    if new_years:
        lines.append(f"新增年度：{', '.join(map(str, new_years))}")
    if added:
        lines.append("新增休市：" + "、".join(f"{k[5:].replace('-', '/')} {n}" for k, n in added))
    if removed:
        lines.append("移除：" + "、".join(k[5:].replace("-", "/") for k in removed))
    msg = "\n".join(lines)
    print(msg)
    # 只有「真的多了休市日」才通知（例如次年行事曆公布、年中新增國定假日）
    if added and "--quiet" not in argv:
        try:
            from dotenv import load_dotenv
            load_dotenv(".env")
            from notify import send_telegram
            send_telegram(msg + "\n請核對；颱風假等臨時休市仍需手動加進 tw_calendar.py。")
        except Exception as e:
            print(f"通知失敗：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
