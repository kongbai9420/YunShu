"""主密钥命令行工具。任何子命令都不会把密钥打印到终端。

    python -m app.keytool path               显示密钥文件位置
    python -m app.keytool init               生成密钥文件（已存在则只显示编号）
    python -m app.keytool fingerprint        显示当前密钥编号
    python -m app.keytool export <目标文件>   导出一份密钥文件用于单独备份（目标不得已存在）
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.crypto import KeyFileError, default_key_path, export_keyring, load_or_create_keyring
from app.settings import Settings


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("path", "init", "fingerprint", "export"):
        print(__doc__)
        return 2
    path = default_key_path()
    data_dir = Settings.from_env().data_dir
    try:
        if argv[0] == "path":
            print(path)
        elif argv[0] == "init":
            ring = load_or_create_keyring(path, data_dir=data_dir)
            print(f"密钥文件：{path}\n当前密钥编号：{ring.current_id}")
        elif argv[0] == "fingerprint":
            ring = load_or_create_keyring(path, data_dir=data_dir, create=False)
            print(ring.current_id)
        else:
            if len(argv) != 2:
                print("用法：python -m app.keytool export <目标文件>")
                return 2
            dest = Path(argv[1])
            if dest.exists():
                print("目标文件已存在，未覆盖")
                return 1
            kid = export_keyring(path, dest)
            print(f"已导出到 {dest}（密钥编号 {kid}）。请与数据库备份分开保存。")
    except KeyFileError as exc:
        print(f"错误：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
