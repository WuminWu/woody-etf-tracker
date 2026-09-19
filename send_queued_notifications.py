# -*- coding: utf-8 -*-
"""
send_queued_notifications.py — 把本輪暫存的單檔「📊 持股更新」通知，依基金規模由大到小發送。

run_update.ps1 在所有爬蟲跑完、產生日報之前呼叫本腳本（先明細、後總結）。
爬蟲在排程模式下只把通知寫進 logs/notify_queue.json（見 notify.send_update_notification）。

可靠性：
- 每送出一則就從佇列移除並存檔，程式中途被中斷也不會重送已送出的訊息
- 某則送失敗就停止，剩下的留到下一輪從同一則接著送，順序不會亂
"""

import logging
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

from notify import load_queue, save_queue, send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


def ordered(entries):
    """規模（億）由大到小；同規模以代號排序，確保每次一致。"""
    return sorted(entries, key=lambda e: (-(e.get("market_cap") or 0), e.get("code", "")))


def main():
    queue = ordered(load_queue())
    if not queue:
        log.info("通知佇列是空的，無需發送。")
        return 0
    log.info("本輪待發送（依規模由大到小）：" + "、".join(
        f"{e['code']}({e.get('market_cap', 0):,.0f}億)" for e in queue))
    remaining = list(queue)
    for e in queue:
        if not send_telegram(e["message"]):
            log.warning(f"{e['code']} 發送失敗；剩下 {len(remaining)} 則留待下一輪依序續送。")
            break
        remaining.remove(e)
        save_queue(remaining)
        log.info(f"已發送 {e['code']}（{e.get('market_cap', 0):,.1f}億）")
    if not remaining:
        log.info(f"本輪 {len(queue)} 則單檔通知已全部發送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
