"""依比例 vs ⭐主動 的分類（etf_core.flow_baseline / flow_class / format_flow_line）。"""
from etf_core import flow_baseline, flow_class, format_flow_line


def _h(code, prev, now):
    return {"code": code, "prevShares": prev, "shares": now, "diffShares": now - prev}


def test_proportional_day():
    # 10 檔都 +5%，其中 9999 +20%（超比例）、8888 減碼（逆勢）、7777 沒動（少於比例）
    hs = [_h(str(2000 + i), 100_000, 105_000) for i in range(10)]
    hs += [_h("9999", 100_000, 120_000), _h("8888", 100_000, 90_000), _h("7777", 100_000, 100_000)]
    base = flow_baseline(hs)
    assert base == 5.0
    assert flow_class(hs[0], base) == "prop"
    assert flow_class(hs[-3], base) == "over"
    assert flow_class(hs[-2], base) == "contra"
    assert flow_class(hs[-1], base) == "under"
    line = format_flow_line({"holdings": hs, "meta": {"flowBaseline": base,
                                                     "totalShares": 1050, "prevTotalShares": 1000}})
    assert "依比例 10" in line and "⭐超比例 1" in line and "⭐逆勢 1" in line and "淨申購 5.0%" in line


def test_normal_day_no_tags():
    hs = [_h(str(2000 + i), 100_000, 100_000) for i in range(10)] + [_h("9999", 100_000, 130_000)]
    base = flow_baseline(hs)
    assert base == 0.0
    assert flow_class(hs[-1], base) is None
    assert format_flow_line({"holdings": hs, "meta": {"flowBaseline": base,
                                                     "totalShares": 1000, "prevTotalShares": 1000}}) == ""


def test_inflow_held_as_cash():
    hs = [_h(str(2000 + i), 100_000, 100_000) for i in range(10)]
    line = format_flow_line({"holdings": hs, "meta": {"flowBaseline": flow_baseline(hs),
                                                     "totalShares": 1087, "prevTotalShares": 1000}})
    assert "淨申購 8.7%" in line and "新資金尚未投入股票" in line
