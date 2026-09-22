#!/usr/bin/env bash
# iBike 控制台启动脚本：自动准备虚拟环境再启动服务。
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "首次运行，正在创建虚拟环境…"
  "$PYTHON" -m venv .venv
fi

# 依赖没装齐就装一次
if ! .venv/bin/python -c "import bleak, aiohttp, qrcode" >/dev/null 2>&1; then
  echo "正在安装依赖…"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec .venv/bin/python main.py "$@"
