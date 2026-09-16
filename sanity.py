# -*- coding: utf-8 -*-
"""
sanity.py — 寫入前的資料合理性守門。

背景：爬蟲的解析器只要碰到來源版面位移，就可能只解析到少數幾檔卻「成功」寫入，
下游（試算表 → 快照 → 日報）會全部跟著錯，而且沒有任何警訊。
既有的規模合理性檢查（totalShares ±50%）攔不到這種「持股檔數驟降」。

規則：新的持股檔數若低於前一個交易日的 60%，視為解析異常。
"""
import glob
import json
import os

DROP_RATIO = 0.6      # 低於前一日的 60% 視為異常
MIN_PREV = 5          # 前一日太少（新基金剛上線）就不判斷


def _prev_holdings_file(code, data_date, holdings_dir="holdings"):
    """回傳 data_date 之前最近一個持股檔 (路徑, 日期)；沒有則 (None, None)。"""
    files = []
    for f in glob.glob(os.path.join(holdings_dir, f"{code}_holdings_*.json")):
        d = os.path.basename(f)[-15:-5]
        if len(d) == 10 and d < data_date:
            files.append((d, f))
    if not files:
        return None, None
    d, f = max(files)
    return f, d


def holdings_count_check(code, new_count, data_date, holdings_dir="holdings"):
    """
    回傳 (ok, msg)。ok=False 代表持股檔數相對前一交易日驟降，應視為解析異常。
    """
    path, prev_date = _prev_holdings_file(code, data_date, holdings_dir)
    if not path:
        return True, "無前一日資料可比對"
    try:
        prev_n = len(json.load(open(path, encoding="utf-8")))
    except Exception:
        return True, "前一日資料讀取失敗，略過比對"
    if prev_n < MIN_PREV:
        return True, f"前一日僅 {prev_n} 檔，不判斷"
    if new_count < prev_n * DROP_RATIO:
        return False, f"持股檔數 {new_count} 檔，較前一交易日（{prev_date}）{prev_n} 檔驟降 {100*(1-new_count/prev_n):.0f}%"
    return True, f"{new_count} 檔（前一日 {prev_n} 檔）"
