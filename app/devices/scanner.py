"""块设备扫描：lsblk 提供拓扑，blkid 做新鲜探测，只保留外接/可热插拔磁盘。"""

from __future__ import annotations

import json
import logging
import re
import subprocess

from app.models import DiskInfo, PartitionInfo

log = logging.getLogger("scanner")

_LSBLK_COLUMNS = (
    "NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTUUID,SERIAL,MODEL,VENDOR,TRAN,RM,HOTPLUG"
)
_PART_NUMBER_RE = re.compile(r"(\d+)$")


def _run(cmd: list[str], timeout: int = 20) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} 退出码 {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout


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
                    bitlocker=fstype.strip().lower() == "bitlocker",
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

    log.debug("扫描到 %d 块外接磁盘：%s", len(disks), [d.name for d in disks])
    return disks
