"""块设备扫描：lsblk 提供拓扑，blkid 做新鲜探测，只保留外接/可热插拔磁盘。"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

from app.models import DiskInfo, PartitionInfo
from app.mounts.commands import dm_name_for

log = logging.getLogger("scanner")

_MAPPER_DIR = Path("/dev/mapper")

_LSBLK_COLUMNS = (
    "NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTUUID,SERIAL,MODEL,VENDOR,TRAN,RM,HOTPLUG"
)
_PART_NUMBER_RE = re.compile(r"(\d+)$")


def _decode(raw: bytes) -> str:
    """命令输出解码：优先 UTF-8；Windows 中文环境的卷标/型号常为 GBK，
    UTF-8 失败时尝试 GBK；仍失败则替换非法字节，保证扫描绝不因编码崩溃。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _run(cmd: list[str], timeout: int = 20) -> str:
    # 按字节读取后自行解码：text=True 会强制 UTF-8 strict，遇到 GBK 卷标直接抛异常
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} 退出码 {proc.returncode}: {_decode(proc.stderr).strip()[:500]}"
        )
    return _decode(proc.stdout)


def _truthy(value: object) -> bool:
    return str(value or "").strip() in {"1", "true", "True", "yes"}


def _probe_partition(path: str) -> dict[str, str]:
    """blkid 深度探测单个分区；读不到（空分区/无签名）时返回空字典。"""
    try:
        out = _run(["blkid", "-p", "-o", "export", path])
    except (RuntimeError, subprocess.SubprocessError) as exc:
        log.debug("blkid 探测 %s 失败：%s", path, exc)
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _partition_number(name: str) -> int:
    match = _PART_NUMBER_RE.search(name)
    return int(match.group(1)) if match else 0


def _mapper_label(vol_key: str) -> str:
    """BitLocker 卷解锁后从 bsbr-* 映射设备补读卷标；未解锁时映射不存在，返回空。

    BitLocker 把卷标连同元数据一起加密，原分区上 blkid 只见 BitLocker 签名、
    永远读不到 LABEL；cryptsetup 解锁生成的明文映射上才是真实 NTFS 卷标。
    """
    return _probe_partition(str(_MAPPER_DIR / dm_name_for(vol_key))).get("LABEL") or ""


def scan() -> list[DiskInfo]:
    """返回当前所有 USB/可热插拔磁盘及其分区。同步方法，调用方请放线程池。"""
    out = _run(["lsblk", "-J", "-b", "-p", "-o", _LSBLK_COLUMNS])
    data = json.loads(out)

    disks: list[DiskInfo] = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        tran = str(dev.get("tran") or "").lower()
        removable = _truthy(dev.get("rm"))
        hotplug = _truthy(dev.get("hotplug"))
        # USB 盘 / 标记可移动 / 内核标记 hotplug（部分 SATA 转接的硬盘盒）
        is_external = tran == "usb" or removable or hotplug
        if not is_external:
            log.debug("跳过非外接磁盘 %s tran=%s rm=%s hotplug=%s",
                      dev.get("name"), tran, dev.get("rm"), dev.get("hotplug"))
            continue

        serial = str(dev.get("serial") or "").strip()
        name = str(dev.get("name") or "").removeprefix("/dev/")
        disk_id = re.sub(r"[^A-Za-z0-9_.-]", "_", serial) if serial else name

        children = dev.get("children") or []
        partitions: list[PartitionInfo] = []
        for child in children:
            if child.get("type") != "part":
                continue
            path = str(child.get("name") or "")
            if not path:
                continue
            part_name = path.removeprefix("/dev/")
            probe = _probe_partition(path)
            fstype = (
                probe.get("TYPE")
                or str(child.get("fstype") or "")
            )
            uuid = probe.get("UUID") or str(child.get("uuid") or "")
            label = probe.get("LABEL") or str(child.get("label") or "")
            partuuid = probe.get("PARTUUID") or str(child.get("partuuid") or "")
            bitlocker = fstype.strip().lower() == "bitlocker"

            # BitLocker 卷标在原分区上加密不可见；卷已解锁时从 bsbr-* 映射补读
            if bitlocker:
                number = _partition_number(part_name)
                mapper_label = _mapper_label(partuuid or uuid or f"{disk_id}-p{number}")
                if mapper_label:
                    label = mapper_label

            partitions.append(
                PartitionInfo(
                    disk_id=disk_id,
                    number=_partition_number(part_name),
                    name=part_name,
                    path=path,
                    size=int(child.get("size") or 0),
                    fstype=fstype,
                    label=label,
                    uuid=uuid,
                    partuuid=partuuid,
                    bitlocker=bitlocker,
                )
            )

        # 无分区表（superfloppy）：文件系统直接写在整盘上，把磁盘本身当作 part1
        dev_fstype = str(dev.get("fstype") or "").strip()
        if not partitions and dev_fstype:
            path = str(dev.get("name") or f"/dev/{name}")
            probe = _probe_partition(path)
            fstype = probe.get("TYPE") or dev_fstype
            uuid = probe.get("UUID") or str(dev.get("uuid") or "")
            label = probe.get("LABEL") or str(dev.get("label") or "")
            bitlocker = fstype.strip().lower() == "bitlocker"
            if bitlocker:
                mapper_label = _mapper_label(uuid or f"{disk_id}-p1")
                if mapper_label:
                    label = mapper_label
            partitions.append(
                PartitionInfo(
                    disk_id=disk_id,
                    number=1,
                    name=name,
                    path=path,
                    size=int(dev.get("size") or 0),
                    fstype=fstype,
                    label=label,
                    uuid=uuid,
                    partuuid="",
                    bitlocker=bitlocker,
                )
            )

        disks.append(
            DiskInfo(
                disk_id=disk_id,
                name=name,
                path=str(dev.get("name") or f"/dev/{name}"),
                vendor=str(dev.get("vendor") or "").strip(),
                model=str(dev.get("model") or "").strip(),
                serial=serial,
                tran=tran,
                size=int(dev.get("size") or 0),
                removable=removable,
                partitions=sorted(partitions, key=lambda p: p.number),
            )
        )

    # 同一硬盘盒的 USB 桥接芯片可能对盒内多块盘上报相同序列号（USB 3.0
    # UAS 桥接下常见，USB 2.0 同一盒子往往正常）：disk_id 撞车会让挂载
    # 目录 / SFTP 路径互相覆盖，表现为两块盘显示同一份数据。对冲突的盘
    # 统一追加内核设备名后缀（与扫描顺序无关）：<序列号>-sdc、<序列号>-sdd
    counts = Counter(d.disk_id for d in disks)
    if any(n > 1 for n in counts.values()):
        fixed: list[DiskInfo] = []
        for d in disks:
            if counts[d.disk_id] <= 1:
                fixed.append(d)
                continue
            new_id = f"{d.disk_id}-{d.name}"
            fixed.append(replace(
                d,
                disk_id=new_id,
                partitions=[replace(p, disk_id=new_id) for p in d.partitions],
            ))
        disks = fixed
        log.warning("检测到多块磁盘序列号相同，已追加设备名后缀区分：%s",
                    [(d.name, d.disk_id) for d in disks])

    log.debug("扫描到 %d 块外接磁盘：%s", len(disks), [d.name for d in disks])
    return disks
