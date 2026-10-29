@echo off
rem Run the 4.0 desktop shell after installing dependencies and building frontend.
chcp 65001 >nul
pushd "%~dp0..\backend"
if not exist ".venv\Scripts\python.exe" (
  echo 请先按 scripts\desktop-guide.md 安装桌面版依赖。
  popd
  exit /b 1
)
".venv\Scripts\python.exe" -m app.desktop
set "desktopExit=%errorlevel%"
popd
exit /b %desktopExit%
