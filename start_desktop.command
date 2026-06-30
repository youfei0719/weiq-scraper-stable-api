#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  osascript -e 'display dialog "未检测到项目虚拟环境 .venv。请先完成依赖安装后再双击启动。" buttons {"确定"} default button "确定" with icon caution'
  exit 1
fi

exec "$PYTHON_BIN" scraper.py
