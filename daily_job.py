#!/usr/bin/env python3
"""每日 15:10 由 launchd 调用（用 python3 直接跑，避免 zsh 没有 Documents 访问权限）：同步分钟数据 -> 重建缓存 -> 回避名单"""
import os, subprocess, sys, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
steps = [["mins_sync.py"], ["scan_live.py", "--build-cache"], ["scan_avoid.py"], ["scan_low.py"]]
for st in steps:
    print(f"{datetime.datetime.now():%H:%M:%S} >>> {' '.join(st)}", flush=True)
    r = subprocess.run([PY] + st, cwd=HERE)
    if r.returncode != 0 and st[0] == "mins_sync.py":
        print("mins_sync 失败，后续步骤跳过", flush=True)
        break
