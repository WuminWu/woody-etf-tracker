"""大額申購隔天「申購款入帳」判斷（asset_allocation.attach_delta → settledInflow）。"""
from asset_allocation import attach_delta, format_alloc_lines, format_settled_line


def _a(unlisted_pct, unlisted_amt, cash_amt, cash_pct=3.0):
    return {"stockPct": 80.0, "cashPct": cash_pct, "cashAmount": cash_amt,
            "unlistedPct": unlisted_pct, "unlistedAmount": unlisted_amt}


def test_00981A_1125_settled():
    # 實際數字：11/24 未列 13.30%（377.5億）、現金 95.2億 → 11/25 未列 −0.51%、現金 456.3億
    prev = _a(13.3, 37_748_360_383, 9_522_550_522, 3.36)
    cur = attach_delta(_a(-0.51, -1_377_423_814, 45_629_953_703, 16.86), prev)
    s = cur["settledInflow"]
    assert round(s["cashIncrease"] / 1e8, 1) == 361.1
    assert round(s["unlistedDecrease"] / 1e8, 1) == 391.3
    line = format_settled_line(cur)
    assert "現金增加 361.1億主要是前一日的申購款入帳" in line and "13.3% → -0.51%" in line
    assert line in format_alloc_lines(cur)


def test_normal_day_no_note():
    prev = _a(0.44, 981_017_381, 8_754_042_890)
    cur = attach_delta(_a(13.3, 37_748_360_383, 9_522_550_522), prev)   # 申購當天：未列變大，不是入帳
    assert "settledInflow" not in cur


def test_unlisted_gone_but_cash_not_up():
    # 未列項目消失但現金沒增加（例如直接拿去買股票）→ 不能說「轉入現金」
    prev = _a(10.0, 30_000_000_000, 9_000_000_000)
    cur = attach_delta(_a(0.2, 600_000_000, 9_500_000_000), prev)
    assert "settledInflow" not in cur


def test_non_ezmoney_source():
    cur = attach_delta({"stockPct": 90.0, "cashPct": 10.0}, {"stockPct": 85.0, "cashPct": 15.0})
    assert "settledInflow" not in cur and format_settled_line(cur) == ""
