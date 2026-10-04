"""SMART 健康信息：调用 smartctl -j（JSON），适配 USB 硬盘盒的多种 SAT 透传方式。

很多 USB-SATA/USB-NVMe 桥接芯片默认 -d sat 不响应，需要换 12/16 字节 SCSI/ATA
命令长度重试；全部失败时返回 unavailable，前端整块隐藏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("smart")

# 依次尝试的 -d 设备类型；None 表示让 smartctl 自动识别
_DEVICE_TYPES: list[str | None] = ["sat", "sat,12", "sat,16", None]
_CACHE_TTL = 600  # 秒；SMART 数据变化很慢，手动刷新可 force 绕过

_cache: dict[str, tuple[float, dict]] = {}


def _attr(table: list[dict], attr_id: int) -> int | None:
    for row in table:
        if row.get("id") == attr_id:
            raw = row.get("raw", {})
            value = raw.get("value") if isinstance(raw, dict) else None
            if isinstance(value, int):
                return value
    return None


def _parse(data: dict) -> dict | None:
    """从 smartctl JSON 提取关心的字段；拿不到任何健康信息时返回 None。"""
    smart_status = data.get("smart_status") or {}
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    temp_block = data.get("temperature") or {}

    healthy = smart_status.get("passed")
    temp = temp_block.get("current")
    if temp is None and table:
        # 194/190 都是常见温度属性
        temp = _attr(table, 194) if _attr(table, 194) is not None else _attr(table, 190)
    power_hours = _attr(table, 9)
    power_cycles = _attr(table, 12)

    if healthy is None and not table and temp is None:
        return None
    return {
        "status": "ok",
        "healthy": bool(healthy) if healthy is not None else None,
        "tempC": temp if isinstance(temp, int) else None,
        "powerOnHours": power_hours,
        "powerCycles": power_cycles,
    }


async def _run_smartctl(device: str, device_type: str | None) -> dict | None:
    cmd = ["smartctl", "-j", "-H", "-A"]
    if device_type:
        cmd += ["-d", device_type]
    cmd.append(device)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
        log.warning("smartctl 执行失败（%s）：%s", device, exc)
        return None
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _parse(data)


async def query_smart(device: str, force: bool = False) -> dict:
    """查询整盘 SMART；带 10 分钟缓存。

    返回：
      ok          —— 含 healthy/tempC/powerOnHours/powerCycles（字段可能为 None）
      unavailable —— 桥接芯片不支持 / smartctl 无法读取
    """
    now = time.monotonic()
    cached = _cache.get(device)
    if not force and cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    result: dict | None = None
    for device_type in _DEVICE_TYPES:
        result = await _run_smartctl(device, device_type)
        if result is not None:
            break

    if result is None:
        result = {"status": "unavailable"}
        log.info("SMART 不可用（硬盘盒可能不支持透传）：%s", device)

    _cache[device] = (now, result)
    return result
