#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

if ! command -v python3 &> /dev/null; then
    osascript -e 'display alert "未检测到 Python 3" message "请先安装 Python 3（或通过 Homebrew: brew install python）后再启动软件。"'
    exit 1
fi

if ! python3 -c "import webview" &> /dev/null; then
    echo "正在安装必要的 GUI 组件库 (pywebview, bottle)..."
    python3 -m pip install pywebview bottle
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python3 src/app.py
