#!/bin/sh
# 用本机可用的 Python 跑脚本：优先项目内 .venv，其次 brew 3.13 / 3.12，再次框架版 3.12 / 3.11
cd "$(dirname "$0")"
for p in ./.venv/bin/python /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 python3; do
  if [ -x "$p" ] || command -v "$p" >/dev/null 2>&1; then
    if "$p" -c "import pandas, requests, pytdx" >/dev/null 2>&1; then exec "$p" "$@"; fi
  fi
done
echo "没有可用的 Python（需要 pandas、requests、pytdx）" >&2; exit 1
