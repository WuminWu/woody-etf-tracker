# -*- coding: utf-8 -*-
"""
notify.py — Telegram 發送單一來源（節流 + 429 洪水重試 + 過長自動分段）。

原本 19 支爬蟲各自有一份 send_telegram，都沒有節流、也沒有 429 重試；
只有 daily_digest 那份是強化版。週五爆量（個股通知 + 日報 + 週報 + 單檔週報）
時，沒保護的訊息會被 Telegram 靜默丟棄 —— 使用者多次回報「已發送卻收不到」。
現在全部共用這一份。

注意：環境變數在「呼叫當下」讀取，因為各爬蟲是先 load_dotenv 才呼叫。
"""
import json
import logging
import os
import time as _time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_last_send_ts = 0.0
_MIN_SEND_GAP = 4.0   # 每則至少間隔（秒）＝15 則/分，安全低於 Telegram 約 20 則/分上限


def _split_message(message, limit=3900):
    """過長訊息以行為界切段（Telegram 單則上限 4096 字，留安全邊界）。"""
    if len(message) <= limit:
        return [message]
    parts, buf = [], ""
    for line in message.split("\n"):
        while len(line) > limit:            # 單行本身就超長：硬切
            if buf:
                parts.append(buf); buf = ""
            parts.append(line[:limit]); line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf); buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        parts.append(buf)
    return parts


def send_telegram(message):
    parts = _split_message(message)
    if len(parts) > 1:
        n = len(parts)
        return all(_send_one(f"（{i}/{n}）\n{p}") for i, p in enumerate(parts, 1))
    return _send_one(message)


def _send_one(message):
    global _last_send_ts
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.warning("Telegram credentials not set - printing message instead.")
        print(message)
        return False
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(4):
        gap = _MIN_SEND_GAP - (_time.time() - _last_send_ts)
        if gap > 0:
            _time.sleep(gap)
        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                _last_send_ts = _time.time()
                ok = json.loads(r.read()).get("ok", False)
                if ok:
                    log.info("Telegram notification sent.")
                return ok
        except urllib.error.HTTPError as e:
            _last_send_ts = _time.time()
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            if e.code == 429:   # 洪水保護：依 retry_after 等待後重試
                wait = body.get("parameters", {}).get("retry_after", 5) + 1
                log.warning(f"Telegram 429 flood control, retry in {wait}s (attempt {attempt+1})")
                _time.sleep(wait)
                continue
            log.warning(f"Telegram HTTP {e.code}: {body}")
            return False
        except Exception as e:
            log.warning(f"Telegram failed: {e}")
            _time.sleep(3)
    log.warning("Telegram: all retries failed.")
    return False
