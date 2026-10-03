#!/bin/bash
set -e
echo "=== Building YunShu (云枢) for macOS ==="
pip install --upgrade pyinstaller pywebview bottle paramiko cryptography
pyinstaller --noconfirm --clean --windowed --name="云枢" \
  --icon="src/assets/app_icon.png" \
  --add-data="src/ui:ui" \
  --add-data="src/assets:assets" \
  --hidden-import=webview \
  --hidden-import=bottle \
  --hidden-import=paramiko \
  --hidden-import=cryptography \
  --hidden-import=configparser \
  --hidden-import=logging \
  --hidden-import=webbrowser \
  --hidden-import=subprocess \
  src/app.py
echo "=== Build Complete! App bundle located at dist/云枢.app ==="
