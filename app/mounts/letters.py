"""盘符注册表：给每个挂载卷分配一个 Windows 风格的唯一盘符。

- 字母池：C~Z（24 个单字母，沿用 Windows 跳过 A/B 软驱的传统），
  用完后进入 Aa~Zz（大写字母+小写字母，26×26=676 个），共 700 个；
  全部用尽时抛 DriveLettersExhausted，拒绝再挂载。
- 分配规则：按卷 key（外接卷 PARTUUID / 远程挂载 id）持久记忆在
  data/drive_letters.json，同一块盘拔出再插回、容器重启后仍是同一盘符；
  没有记忆的卷按字母池顺序取第一个空闲盘符。
- 只有外接卷和远程挂载使用盘符；本地存储（Docker bind，目录名来自
  compose 映射，运行时不可改）不参与。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.config import DATA_DIR

log = logging.getLogger("mount.letters")

_UPPERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWERS = "abcdefghijklmnopqrstuvwxyz"

# C~Z 跳过 A、B（软驱传统）；单字母用完接 Aa~Zz
POOL: list[str] = [c for c in _UPPERS if c not in "AB"] + [
    u + l for u in _UPPERS for l in _LOWERS
]

DEFAULT_PATH = DATA_DIR / "drive_letters.json"


class DriveLettersExhausted(RuntimeError):
    """盘符已全部用尽（上限 700 个）。"""


class DriveLetterRegistry:
    """卷 key -> 盘符 的持久映射（线程安全）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("盘符记录 %s 读取失败，重新从头分配", self._path)
            return
        if isinstance(data, dict):
            self._map = {
                str(k): v
                for k, v in data.items()
                if isinstance(v, str) and v in POOL
            }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._map, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def letter_of(self, key: str) -> str | None:
        """key 已记忆的盘符；未分配过返回 None。"""
        with self._lock:
            return self._map.get(key)

    def allocate(self, key: str) -> str:
        """取 key 记忆的盘符，没有则分配池中第一个空闲盘符并记忆。

        盘符一经分配永久记忆（卷拔出也保留，Windows 风格），
        只有显式 release 才释放。
        """
        with self._lock:
            saved = self._map.get(key)
            if saved:
                return saved
            used = set(self._map.values())
            for letter in POOL:
                if letter not in used:
                    self._map[key] = letter
                    self._save()
                    log.info("分配盘符 %s -> %s", letter, key)
                    return letter
            raise DriveLettersExhausted(
                f"盘符已用尽（上限 {len(POOL)} 个），无法再挂载新卷"
            )

    def release(self, key: str) -> None:
        """删除 key 的盘符记忆（删除远程挂载时调用，盘符回收复用）。"""
        with self._lock:
            if self._map.pop(key, None) is not None:
                self._save()

    def reserve(self, key: str, letter: str) -> bool:
        """预占指定盘符（本地映射目录名恰为盘符形态时防冲突）。

        字母无效、key 已记忆其他盘符、或盘符已被其他卷占用时不抢占，
        返回 False 由调用方告警。
        """
        if letter not in POOL:
            return False
        with self._lock:
            current = self._map.get(key)
            if current == letter:
                return True
            if current is not None or letter in set(self._map.values()):
                return False
            self._map[key] = letter
            self._save()
            log.info("预占盘符 %s -> %s", letter, key)
            return True
