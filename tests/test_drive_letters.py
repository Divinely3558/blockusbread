"""盘符注册表：字母池顺序、持久记忆、释放与耗尽。"""

from __future__ import annotations

import json

import pytest

from app.mounts import letters as letters_mod
from app.mounts.letters import DriveLetterRegistry, DriveLettersExhausted


def test_pool_starts_at_c_skips_a_b():
    """字母池从 C 开始（跳过 A/B 软驱传统），单字母用完接 Aa~Zz。"""
    assert letters_mod.POOL[0] == "C"
    assert letters_mod.POOL[23] == "Z"
    assert letters_mod.POOL[24] == "Aa"
    assert letters_mod.POOL[25] == "Ab"
    assert letters_mod.POOL[-1] == "Zz"
    assert len(letters_mod.POOL) == 24 + 26 * 26
    assert len(set(letters_mod.POOL)) == len(letters_mod.POOL)   # 无重复


def test_allocate_in_pool_order(tmp_path):
    reg = DriveLetterRegistry(path=tmp_path / "letters.json")
    assert reg.allocate("PARTUUID-1") == "C"
    assert reg.allocate("PARTUUID-2") == "D"
    # 同一个 key 重复分配返回同一盘符
    assert reg.allocate("PARTUUID-1") == "C"


def test_allocate_persists_across_instances(tmp_path):
    """盘符记忆持久化：重建注册表后同一卷仍是同一盘符（重插拔/重启不变）。"""
    path = tmp_path / "letters.json"
    first = DriveLetterRegistry(path=path)
    d1 = first.allocate("PARTUUID-1")
    d2 = first.allocate("remote:abc")
    assert (d1, d2) == ("C", "D")

    second = DriveLetterRegistry(path=path)
    assert second.letter_of("PARTUUID-1") == "C"
    assert second.letter_of("remote:abc") == "D"
    assert second.allocate("PARTUUID-1") == "C"   # 已记忆的卷不占新盘符
    assert second.allocate("new-key") == "E"      # 新卷接着往后分


def test_release_frees_letter_for_reuse(tmp_path):
    reg = DriveLetterRegistry(path=tmp_path / "letters.json")
    assert reg.allocate("k1") == "C"
    assert reg.allocate("k2") == "D"
    reg.release("k1")
    assert reg.letter_of("k1") is None
    assert reg.allocate("k3") == "C"   # 释放后盘符回收复用


def test_reserve_pins_letter_and_blocks_allocation(tmp_path):
    """预占：本地目录名恰为盘符时钉住该字母，后续分配自动跳过。"""
    reg = DriveLetterRegistry(path=tmp_path / "letters.json")
    assert reg.reserve("local:C", "C") is True
    assert reg.letter_of("local:C") == "C"
    assert reg.allocate("PARTUUID-1") == "D"   # C 已被预占，跳过
    assert reg.reserve("other", "C") is False  # 不抢占已占用盘符
    assert reg.reserve("local:C", "C") is True  # 重复预占幂等
    assert reg.reserve("k", "1") is False       # 非法盘符拒绝


def test_reserve_persists(tmp_path):
    path = tmp_path / "letters.json"
    DriveLetterRegistry(path=path).reserve("local:D", "D")
    reg = DriveLetterRegistry(path=path)
    assert reg.allocate("PARTUUID-1") == "C"
    assert reg.allocate("PARTUUID-2") == "E"   # D 持久预占，跳过


def test_allocate_reuses_saved_letter_even_if_taken_in_file(tmp_path):
    """记忆盘符优先：同一 key 始终拿回自己的盘符。"""
    path = tmp_path / "letters.json"
    path.write_text(json.dumps({"k1": "Z"}), encoding="utf-8")
    reg = DriveLetterRegistry(path=path)
    assert reg.allocate("k1") == "Z"
    assert reg.allocate("k2") == "C"   # 新卷跳过被记忆占用的 Z


def test_exhausted_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(letters_mod, "POOL", ["C"])
    reg = DriveLetterRegistry(path=tmp_path / "letters.json")
    assert reg.allocate("k1") == "C"
    with pytest.raises(DriveLettersExhausted):
        reg.allocate("k2")


def test_corrupt_file_starts_fresh(tmp_path):
    """记录文件损坏时从头分配，不崩溃。"""
    path = tmp_path / "letters.json"
    path.write_text("{broken", encoding="utf-8")
    reg = DriveLetterRegistry(path=path)
    assert reg.allocate("k1") == "C"
