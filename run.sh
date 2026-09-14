#!/bin/sh
# 用本机可用的 Python 跑脚本：优先 3.12（brew），其次 3.11（框架版），再次 python3
cd "$(dirname "$0")"
for p in /opt/homebrew/bin/python3.12 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c "import pandas, requests" >/dev/null 2>&1; then exec "$p" "$@"; fi
done
echo "没有可用的 Python（需要 pandas、requests）" >&2; exit 1
