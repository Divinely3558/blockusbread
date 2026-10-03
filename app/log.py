"""日志：INFO 简洁（谁/对什么/做了什么/结果），DEBUG 输出详细过程；同时写文件。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str, log_dir: Path) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 处理器各自控制级别

    stream = logging.StreamHandler()
    stream.setLevel(level)
    stream.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    # 文件里始终保留 DEBUG，便于事后排查（不含凭据，凭据由调用方保证脱敏）
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(file_handler)

    _CONFIGURED = True
