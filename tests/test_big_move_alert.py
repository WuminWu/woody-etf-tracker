# -*- coding: utf-8 -*-
"""big_move_alert 的偵測／去重規則（合成 snapshot，不讀正式資料）。

    python -m pytest tests/test_big_move_alert.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import big_move_alert as bma  # noqa: E402

D = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"]
FILLER = [{"code": f"{1100 + i}", "name": f"F{i}", "shares": 10000, "prevShares": 10000,
           "yestWeight": 1.0, "todayWeight": 1.0, "diffShares": 0, "diffAmount": 0} for i in range(10)]


def h(code, yw, tw, shares, prev, amt=0.0):
    return {"code": code, "name": code, "shares": shares, "prevShares": prev,
            "yestWeight": yw, "todayWeight": tw, "diffShares": shares - prev, "diffAmount": amt}


def snap(per_etf):
    return {etf: {"meta": {"totalMarketCap": 100.0}, "holdings": FILLER + rows}
            for etf, rows in per_etf.items()}


def kinds(events):
    return {(e["etf"], e["code"], e["kind"]) for e in events}


def test_build_over_three_days():
    # 每天只加一點（單日看都不大），3 日累計 0 → 2.4%
    days = [(D[0], snap({"A": []})),
            (D[1], snap({"A": [h("2330", 0, 0.8, 800, 0, 1e8)]})),
            (D[2], snap({"A": [h("2330", 0.8, 1.6, 1600, 800, 1e8)]})),
            (D[3], snap({"A": [h("2330", 1.6, 2.4, 2400, 1600, 1e8)]}))]
    ev = bma.detect(days)
    assert kinds(ev) == {("A", "2330", "build")}
    assert ev[0]["date"] == D[3] and ev[0]["w0"] == 0 and abs(ev[0]["cum"] - 3e8) < 1


def test_slow_build_outside_window_ignored():
    # 4 個交易日才從 0 加到 2.2%：3 日區間的起點已是 0.6%（≥0.5%），不算「短時間」
    days = [(D[0], snap({"A": [h("2330", 0.0, 0.6, 600, 0)]})),
            (D[1], snap({"A": [h("2330", 0.6, 1.0, 1000, 600)]})),
            (D[2], snap({"A": [h("2330", 1.0, 1.6, 1600, 1000)]})),
            (D[3], snap({"A": [h("2330", 1.6, 2.2, 2200, 1600)]}))]
    assert bma.detect(days, eval_dates=1) == []


def test_cut_by_weight_and_by_shares():
    days = [(D[0], snap({"A": [h("2330", 3.0, 3.0, 3000, 3000), h("2454", 4.0, 4.0, 4000, 4000)]})),
            (D[1], snap({"A": [h("2330", 3.0, 0.3, 300, 3000, -2e8), h("2454", 4.0, 1.2, 1100, 4000)]}))]
    assert kinds(bma.detect(days)) == {("A", "2330", "cut"), ("A", "2454", "cut")}


def test_share_drop_without_weight_drop_is_not_cut():
    # 減資／反分割：股數少 80% 但權重不變，不是砍倉
    days = [(D[0], snap({"A": [h("2330", 3.0, 3.0, 5000, 5000)]})),
            (D[1], snap({"A": [h("2330", 3.0, 2.9, 1000, 5000)]}))]
    assert bma.detect(days) == []


def test_cleared_row_missing_is_cut_with_estimated_amount():
    # 有些來源出清後直接不列這檔：前一日 2.5%、規模 100 億 → 約 -2.5 億
    days = [(D[0], snap({"A": [h("2330", 2.5, 2.5, 3000, 3000)]})),
            (D[1], snap({"A": []}))]
    ev = bma.detect(days)
    assert kinds(ev) == {("A", "2330", "cut")}
    assert ev[0]["cleared"] and ev[0]["cum_est"] and abs(ev[0]["cum"] + 2.5e8) < 1
    # 再過一天仍然沒有這檔，不應在新的一天又算一次
    days.append((D[2], snap({"A": []})))
    assert [e["date"] for e in bma.detect(days, eval_dates=1)] == []


def test_truncated_holdings_not_treated_as_clearance():
    # 來源故障只抓到一半持股：消失的那些不是被出清
    full = [h(f"{2300 + i}", 2.5, 2.5, 1000, 1000) for i in range(20)]
    days = [(D[0], {"A": {"meta": {"totalMarketCap": 100.0}, "holdings": full}}),
            (D[1], {"A": {"meta": {"totalMarketCap": 100.0}, "holdings": full[:8]}})]
    assert bma.detect(days) == []


def test_first_day_etf_and_futures_ignored():
    new_etf = [h(f"{2000 + i}", 0, 5.0, 1000, 0) for i in range(12)]
    days = [(D[0], {"N": {"meta": {}, "holdings": new_etf}}),
            (D[1], snap({"A": [h("TX", 0, 3.0, 10, 0), h("2330", 1.0, 1.0, 1, 1)]}))]
    days[0][1].update(snap({"A": [h("2330", 1.0, 1.0, 1, 1)]}))
    assert bma.detect(days) == []


def test_dedup_and_escalation():
    e = {"date": D[3], "etf": "A", "code": "2330", "kind": "build", "w1": 2.4}
    state = {"A|2330|build": {"date": D[2], "w1": 2.2}}
    assert bma.filter_new([e], state) == []
    e2 = dict(e, w1=4.5)
    assert bma.filter_new([e2], state) == [(e2, True)]
    old = {"A|2330|build": {"date": "2026-10-01", "w1": 2.2}}
    assert bma.filter_new([e], old) == [(e, False)]


def test_group_alert_merges_three_etfs():
    rows = lambda: [h("2330", 0, 2.5, 2500, 0, 1e8)]  # noqa: E731
    days = [(D[0], snap({"A": [], "B": [], "C": []})),
            (D[1], snap({"A": rows(), "B": rows(), "C": rows()}))]
    ev = bma.detect(days)
    msg, groups = bma.render(bma.filter_new(ev, {}), ev, {})
    assert "👥" in msg and "3 家同時快速建倉" in msg
    assert "🚀" not in msg          # 已併入集體那一則，不再逐檔列
    state = {}
    bma.mark_sent(state, bma.filter_new(ev, {}), groups)
    assert bma.render(bma.filter_new(ev, state), ev, state) is None
