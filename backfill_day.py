# -*- coding: utf-8 -*-
"""
backfill_day.py — 補抓「某一個交易日」的持股資料（用於排程漏跑）。

背景：本機排程若因當天沒登入 / 電腦關機 / Windows Update 重開機而未執行，
該交易日的資料就會缺一天。各爬蟲的 main() 都以 `datetime.now(+8)` 判斷「今天」，
並要求來源揭露日 == 今天才處理。本工具在呼叫 main() 前，暫時把該模組的
`datetime` 換成回傳指定日期的子類別，讓爬蟲「以為」今天是補抓日，
即可完整重用原有流程（下載 → 解析 → 比對前一日 → 寫 data/holdings → 試算表 → Telegram）。

前提：來源網站當下仍揭露該日資料（多數來源只保留「最新揭露日」，
      所以補抓要在下一個交易日揭露前完成，例如週末補上週五）。

用法：
    python backfill_day.py 2026-09-11                 # 補所有台股檔
    python backfill_day.py 2026-09-11 00981A 00403A   # 只補指定檔
"""
import importlib
import sys
import traceback
from datetime import datetime as _dt, date

# 台股（當日揭露）ETF；海外檔為 T+1，日期語意不同，不納入預設批次
TW_FUNDS = [
    "00981A", "00400A", "00403A", "00980A", "00985A", "00991A", "00992A",
    "00982A", "00987A", "00993A", "00995A", "00996A", "00405A", "00407A",
]


def _make_fake_datetime(target, hour=18, minute=1):
    """回傳一個 datetime 子類別，其 now() 固定回傳 target 當天的指定時刻。"""
    class FakeDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(target.year, target.month, target.day, hour, minute, 0)
            return base.replace(tzinfo=tz) if tz is not None else base
    return FakeDateTime


def backfill_one(code, target):
    """對單一 ETF 執行補抓；回傳 (code, ok, message)。"""
    name = f"check_and_update_{code}"
    try:
        mod = importlib.import_module(name)
    except Exception as e:
        return code, False, f"import 失敗: {e}"

    if not hasattr(mod, "datetime") or not hasattr(mod, "main"):
        return code, False, "模組沒有 datetime/main，無法套用日期覆寫"

    # etf_core 也要一起覆寫：爬蟲的「今天」有一部分是透過 etf_core.today_tw()
    # 取得（共用核心重構後），只改 mod.datetime 會讓那些判斷仍看到真實日期。
    import etf_core
    fake = _make_fake_datetime(target)
    original, original_core = mod.datetime, etf_core.datetime
    mod.datetime = fake
    etf_core.datetime = fake
    try:
        mod.main()
        return code, True, "完成"
    except SystemExit as e:
        return code, True, f"main() 以 exit({e.code}) 結束"
    except Exception as e:
        return code, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    finally:
        mod.datetime = original
        etf_core.datetime = original_core


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    target = date.fromisoformat(sys.argv[1])
    codes = sys.argv[2:] or TW_FUNDS

    print(f"=== 補抓目標日：{target}　共 {len(codes)} 檔 ===")
    results = []
    for code in codes:
        print(f"\n--- {code} ---", flush=True)
        results.append(backfill_one(code, target))

    # 註：主控台可能是 cp950，摘要一律用 ASCII 標記，避免 UnicodeEncodeError
    print("\n=== 結果 ===")
    for code, ok, msg in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {code}: {msg.splitlines()[0]}")
    failed = [c for c, ok, _ in results if not ok]
    print(f"\n成功 {len(results) - len(failed)} / {len(results)}"
          + (f"；失敗: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
