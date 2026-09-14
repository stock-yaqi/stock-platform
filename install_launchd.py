#!/usr/bin/env python3
"""
在当前机器上生成并加载全部 launchd 定时任务（路径和 Python 解释器按本机自动填）。

    python3 install_launchd.py            # 生成 ~/Library/LaunchAgents/com.qyhdt.stock-*.plist 并加载
    python3 install_launchd.py --uninstall
    python3 install_launchd.py --status

任务：
    stock-live-scan   每 60 秒 scan_live.py（脚本自判交易时段）
    stock-mins-sync   工作日 15:10 daily_job.py（同步分钟数据 → 缓存 → 回避名单）
    stock-events      工作日 07:40 / 14:00 scan_events.py --auto
    stock-brief       工作日 08:30 scan_brief.py
    stock-awake       工作日 08:20 caffeinate 防闲置睡眠到 15:30（台式机常开可不需要，仍会装上，无害）
"""
import argparse
import os
import plistlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOGS = os.path.join(HERE, "mins", "_logs")
AGENTS = os.path.expanduser("~/Library/LaunchAgents")
UID = os.getuid()
ENV = {"PYTHONIOENCODING": "utf-8", "LANG": "zh_CN.UTF-8"}


def weekdays(hour, minute):
    return [{"Weekday": w, "Hour": hour, "Minute": minute} for w in range(1, 6)]


JOBS = {
    "com.qyhdt.stock-live-scan": {"ProgramArguments": [PY, os.path.join(HERE, "scan_live.py")], "StartInterval": 60, "RunAtLoad": False, "log": "live_launchd"},
    "com.qyhdt.stock-mins-sync": {"ProgramArguments": [PY, os.path.join(HERE, "daily_job.py")], "StartCalendarInterval": weekdays(15, 10), "log": "launchd"},
    "com.qyhdt.stock-events": {"ProgramArguments": [PY, os.path.join(HERE, "scan_events.py"), "--auto"], "StartCalendarInterval": weekdays(7, 40) + weekdays(14, 0), "log": "events_launchd"},
    "com.qyhdt.stock-brief": {"ProgramArguments": [PY, os.path.join(HERE, "scan_brief.py")], "StartCalendarInterval": weekdays(8, 30), "log": "brief_launchd"},
    "com.qyhdt.stock-awake": {"ProgramArguments": ["/usr/bin/caffeinate", "-i", "-s", "-t", "25800"], "StartCalendarInterval": weekdays(8, 20), "log": None},
}


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True)


def install():
    os.makedirs(AGENTS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    for label, spec in JOBS.items():
        d = {"Label": label, "ProgramArguments": spec["ProgramArguments"], "WorkingDirectory": HERE, "EnvironmentVariables": ENV}
        for k in ("StartInterval", "StartCalendarInterval", "RunAtLoad"):
            if k in spec:
                d[k] = spec[k]
        if spec["log"]:
            d["StandardOutPath"] = os.path.join(LOGS, spec["log"] + ".out")
            d["StandardErrorPath"] = os.path.join(LOGS, spec["log"] + ".err")
        path = os.path.join(AGENTS, label + ".plist")
        sh("launchctl", "bootout", f"gui/{UID}/{label}")
        with open(path, "wb") as f:
            plistlib.dump(d, f)
        r = sh("launchctl", "bootstrap", f"gui/{UID}", path)
        print(f"{'OK ' if r.returncode == 0 else 'ERR'} {label}  {r.stderr.strip()}")


def uninstall():
    for label in JOBS:
        sh("launchctl", "bootout", f"gui/{UID}/{label}")
        p = os.path.join(AGENTS, label + ".plist")
        if os.path.exists(p):
            os.remove(p)
        print("removed", label)


def status():
    for label in JOBS:
        r = sh("launchctl", "print", f"gui/{UID}/{label}")
        if r.returncode != 0:
            print(f"--  {label}: 未加载")
            continue
        info = {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in r.stdout.splitlines() if "=" in l and any(x in l for x in ("state", "last exit", "runs")))}
        print(f"OK  {label}: {info}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    print(f"目录 {HERE}\nPython {PY}\n")
    if a.uninstall:
        uninstall()
    elif a.status:
        status()
    else:
        install()
