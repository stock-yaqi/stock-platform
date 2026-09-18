#!/usr/bin/env python3
"""
常驻调度器：不依赖 launchd，一个进程按钟点跑全部任务。用于 macOS 上 launchd 无法访问外接卷（/Volumes/...）的机器。

    nohup python3 scheduler.py > /dev/null 2>&1 &      # 通过 SSH 启动（SSH 会话起的进程有完整磁盘权限，nohup 后退出 SSH 也继续跑）
    python3 scheduler.py --status                        # 看是否在跑
    python3 scheduler.py --stop

任务表（工作日）：
    每 10 分钟      scan_live.py             （对齐整 10 分钟：09:30、09:40…；脚本自判交易时段 / 交易日，非交易时段秒退）
    每 1 分钟       scan_hold.py             （09:25-15:02，持仓冲高监控；没持仓秒退）
    常驻            webhook.py               （持仓回执 Web 服务，端口 8085，挂了自动拉起；非交易日也开着）
    07:40 / 14:00   scan_events.py --auto
    08:30           scan_brief.py
    15:10           daily_job.py             （同步分钟数据 → 缓存 → 回避名单）
日志：mins/_logs/scheduler.log；单实例锁：mins/_meta/scheduler.lock（含 pid）
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOGS = os.path.join(HERE, "mins", "_logs")
META = os.path.join(HERE, "mins", "_meta")
LOCK = os.path.join(META, "scheduler.lock")
LIVE_EVERY_MIN = 10   # 盘中扫描间隔（分钟），对齐到整点分钟
HOLD_WINDOW = ("09:25", "15:02")   # 持仓冲高监控的时段，每分钟一次
CALENDAR = [  # (HH:MM, argv)
    ("07:40", ["scan_events.py", "--auto"]),
    ("08:30", ["scan_brief.py"]),
    ("14:00", ["scan_events.py", "--auto"]),
    ("15:10", ["daily_job.py"]),
]


def log(msg):
    os.makedirs(LOGS, exist_ok=True)
    line = f"{datetime.now():%m-%d %H:%M:%S} [scheduler] {msg}"
    with open(os.path.join(LOGS, "scheduler.log"), "a") as f:
        f.write(line + "\n")


def run(argv, wait=True, quiet=False):
    if not quiet:
        log("run " + " ".join(argv))
    with open(os.path.join(LOGS, "scheduler_tasks.out"), "a") as out:
        out.write(f"\n===== {datetime.now():%m-%d %H:%M:%S} {' '.join(argv)}\n")
        out.flush()
        p = subprocess.Popen([PY] + [os.path.join(HERE, argv[0])] + argv[1:], cwd=HERE, stdout=out, stderr=subprocess.STDOUT)
        if wait:
            p.wait()
            log(f"done {argv[0]} exit={p.returncode}")
        return p


def running_pid():
    try:
        pid = int(open(LOCK).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    os.makedirs(META, exist_ok=True)
    pid = running_pid()
    if a.status:
        print(f"scheduler 在跑，pid {pid}" if pid else "scheduler 没有在跑")
        return
    if a.stop:
        if pid:
            os.kill(pid, signal.SIGTERM)
            print("已停止", pid)
        else:
            print("没有在跑")
        return
    if pid:
        print(f"已有实例在跑（pid {pid}），退出")
        return
    open(LOCK, "w").write(str(os.getpid()))
    log(f"启动 pid={os.getpid()} python={PY}")
    done_today = {}  # key -> date
    live_proc = None
    last_live_slot = None
    hold_proc = None
    last_hold_slot = None
    web_proc = None
    last_web_start = 0.0
    while True:
        now = datetime.now()
        hm = now.strftime("%H:%M")
        # 持仓回执服务：常驻，挂了 20 秒后重拉（晚上也要能点「我已买入」）
        if (web_proc is None or web_proc.poll() is not None) and time.time() - last_web_start > 20:
            if web_proc is not None:
                log(f"webhook 退出 code={web_proc.returncode}，重启")
            last_web_start = time.time()
            try:
                web_proc = run(["webhook.py"], wait=False)
            except Exception as e:
                log(f"webhook 启动失败 {e!r}")
        if now.weekday() < 5:
            for t, argv in CALENDAR:
                key = t + argv[0]
                if hm >= t and done_today.get(key) != now.date():
                    # 只补最近 5 分钟内错过的；启动太晚的不补，避免下午起进程把早上的都跑一遍
                    tm = int(t[:2]) * 60 + int(t[3:])
                    if now.hour * 60 + now.minute - tm <= 5:
                        done_today[key] = now.date()
                        try:
                            run(argv, wait=(argv[0] != "daily_job.py"))
                        except Exception as e:
                            log(f"任务异常 {argv}: {e!r}")
                    else:
                        done_today[key] = now.date()
            slot = (now.date(), now.hour, now.minute // LIVE_EVERY_MIN)
            if now.minute % LIVE_EVERY_MIN == 0 and slot != last_live_slot and (live_proc is None or live_proc.poll() is not None):
                last_live_slot = slot
                try:
                    live_proc = run(["scan_live.py"], wait=False)
                except Exception as e:
                    log(f"scan_live 异常 {e!r}")
            # 持仓冲高监控：每分钟一次（脚本没持仓 / 非交易日会秒退，日志安静）
            slot_h = (now.date(), now.hour, now.minute)
            if HOLD_WINDOW[0] <= hm <= HOLD_WINDOW[1] and slot_h != last_hold_slot and (hold_proc is None or hold_proc.poll() is not None):
                last_hold_slot = slot_h
                try:
                    hold_proc = run(["scan_hold.py"], wait=False, quiet=True)
                except Exception as e:
                    log(f"scan_hold 异常 {e!r}")
        time.sleep(5)


if __name__ == "__main__":
    main()
