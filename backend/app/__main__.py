"""python -m app 等同于 yunshu 命令（见 app/cli.py）。"""
from app.cli import main

raise SystemExit(main())
