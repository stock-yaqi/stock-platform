#!/bin/zsh
# 每日 15:10 由 launchd 调用：先同步分钟数据，再跑创业板资金进场扫描
cd "$(dirname "$0")"
PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
$PY mins_sync.py && $PY scan_inflow.py --board cyb
