#!/bin/bash
set -e
echo "=== Building Dell Fan Sense for macOS ==="
pip install --upgrade pyinstaller pywebview bottle
pyinstaller --noconfirm --clean --windowed --name="DellFanSense" \
  --icon="src/assets/app_icon.png" \
  --add-data="src/ui:ui" \
  --add-data="src/assets:assets" \
  --hidden-import=webview \
  --hidden-import=bottle \
  --hidden-import=configparser \
  --hidden-import=logging \
  --hidden-import=webbrowser \
  --hidden-import=subprocess \
  src/app.py
echo "=== Build Complete! App bundle located at dist/DellFanSense.app ==="
