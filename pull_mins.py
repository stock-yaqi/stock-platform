#!/usr/bin/env python3
"""
从 Mac Studio 把分钟数据同步一份到本地（笔记本用，作为备份和本地分析用）。
    python3 pull_mins.py            # rsync 远端 mins/ -> 本地 mins/（只拉新增和变化的文件）
远端：sales@192.168.3.21:/Volumes/NewVolume/stock-a/mins/
本地 launchd：com.qyhdt.stock-pull，工作日 16:30（远端 15:10 同步完之后）。
"""
import os, subprocess, sys, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE = "sales@192.168.3.21:/Volumes/NewVolume/stock-a/mins/"
LOCAL = os.path.join(HERE, "mins") + "/"
os.makedirs(LOCAL, exist_ok=True)
logf = os.path.join(LOCAL, "_logs", "pull.log")
os.makedirs(os.path.dirname(logf), exist_ok=True)
t0 = datetime.datetime.now()
r = subprocess.run(["rsync", "-a", "--exclude", "_logs/", "--exclude", "_meta/live.lock", "--exclude", "_meta/scheduler.lock",
                    "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15", REMOTE, LOCAL], capture_output=True, text=True)
msg = f"{t0:%Y-%m-%d %H:%M} pull exit={r.returncode} 耗时 {(datetime.datetime.now()-t0).seconds}s {r.stderr.strip()[:200]}"
print(msg)
with open(logf, "a") as f:
    f.write(msg + "\n")
sys.exit(r.returncode)
