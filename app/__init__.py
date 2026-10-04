"""BlockUSBRead — 在容器中解密挂载 BitLocker USB 硬盘，并通过 SFTP 共享读取。"""

from pathlib import Path


def _read_version() -> str:
    """版本号以仓库根目录 VERSION 文件为唯一来源。"""
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


__version__ = _read_version()
