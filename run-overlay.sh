#!/usr/bin/env bash
# 启动悬浮窗（命令行方式；网页上也有「悬浮显示」按钮，一般用那个就行）。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "找不到 $PYTHON —— 先跑一次 ./run.sh 让它把虚拟环境建起来，"
  echo "或者用 PYTHON=/path/to/python ./run-overlay.sh 指定。"
  exit 1
fi
exec "$PYTHON" overlay.py "$@"
