# -*- coding: utf-8 -*-
"""
run_step.py — 以逾時保護執行排程裡的一個步驟（爬蟲、git pull/push…）。

為什麼：run_update.ps1 原本直接 `& python 爬蟲.py`、`git push`，沒有任何逾時。
  - 2026-07-07 20:30 git push 卡了 19.1 分鐘
  - 2026-10-08 21:00 git push 在等登入視窗（排程沒有桌面），到 10/14 都還掛著
失敗通知 run_summary.py 排在最後，卡住時就不會有任何通知。

用法：
    python run_step.py --timeout 240 check_and_update_00981A.py
    python run_step.py --timeout 180 -- git push
逾時就把整個程序樹（含 Playwright 開的 Chromium）結束，結束碼 124（同 GNU timeout）。
子程序的輸出（stderr 併入 stdout）原封不動轉出，日誌編碼與直接執行時相同。
"""
import os
import subprocess
import sys
import threading

TIMEOUT_EXIT = 124


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(proc.pid, 9)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _pump(src, dst):
    for chunk in iter(lambda: src.readline(), b""):
        dst.write(chunk)
        dst.flush()


def _say(msg):
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.buffer.write((msg + "\n").encode(enc, errors="replace"))
    sys.stdout.buffer.flush()


def run(cmd, timeout, env=None, out=None):
    """執行 cmd（list），回傳結束碼；逾時回 TIMEOUT_EXIT。
    env：子程序的環境變數（預設沿用目前的）；out：輸出寫到哪（binary file-like，預設 stdout）。"""
    out = out or sys.stdout.buffer
    if cmd and cmd[0].lower().endswith(".py"):
        cmd = [sys.executable] + cmd
    kw = {}
    if os.name != "nt":
        kw["start_new_session"] = True     # 讓 killpg 能一次結束整個程序群組
    # 不緩衝：日誌照實際先後順序，而且被逾時砍掉時，卡住之前印的訊息不會留在緩衝區裡遺失
    env = dict(os.environ if env is None else env, PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kw)
    except OSError as e:
        _say(f"[run_step] 無法執行 {cmd!r}：{e}")
        return 127
    reader = threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
        _say(f"[run_step] 逾時 {timeout:.0f} 秒，已強制結束：{' '.join(cmd[-3:])}")
        return TIMEOUT_EXIT
    reader.join(timeout=30)
    return code


def main(argv):
    timeout = 240.0
    args = list(argv)
    if len(args) >= 2 and args[0] == "--timeout":
        timeout = float(args[1])
        args = args[2:]
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        _say(__doc__)
        return 2
    return run(args, timeout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
