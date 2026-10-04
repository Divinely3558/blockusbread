"""pytest 公共夹具：模拟 lsblk -J 输出与 blkid 探测结果。"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def lsblk_payload() -> str:
    """三块盘：内置 SATA（应被过滤）、USB 单分区盘、无分区表 superfloppy 盘。"""
    return json.dumps(
        {
            "blockdevices": [
                {
                    "name": "/dev/sda",
                    "size": 512_110_190_592,
                    "type": "disk",
                    "fstype": None,
                    "label": None,
                    "uuid": None,
                    "partuuid": None,
                    "serial": "INTERNAL001",
                    "model": "Built-in SSD",
                    "vendor": "ATA",
                    "tran": "sata",
                    "rm": False,
                    "hotplug": False,
                    "children": [
                        {
                            "name": "/dev/sda1",
                            "size": 512_110_190_592,
                            "type": "part",
                            "fstype": "ext4",
                            "label": "root",
                            "uuid": "u-sda1",
                            "partuuid": "pu-sda1",
                        }
                    ],
                },
                {
                    "name": "/dev/sdb",
                    "size": 1_000_204_880_384,
                    "type": "disk",
                    "fstype": None,
                    "label": None,
                    "uuid": None,
                    "partuuid": None,
                    "serial": "USB-DISK-001",
                    "model": "Portable",
                    "vendor": "Kingston",
                    "tran": "usb",
                    "rm": True,
                    "hotplug": False,
                    "children": [
                        {
                            "name": "/dev/sdb1",
                            "size": 1_000_203_856_896,
                            "type": "part",
                            "fstype": "bitlocker",
                            "label": None,
                            "uuid": None,
                            "partuuid": "pu-sdb1",
                        }
                    ],
                },
                {
                    "name": "/dev/sdc",
                    "size": 32_010_927_104,
                    "type": "disk",
                    "fstype": "exfat",
                    "label": "备份盘",
                    "uuid": "u-sdc",
                    "partuuid": None,
                    "serial": "SUPER-FLOPPY",
                    "model": "Stick",
                    "vendor": "SanDisk",
                    "tran": "usb",
                    "rm": True,
                    "hotplug": False,
                },
            ]
        },
        ensure_ascii=False,
    )


@pytest.fixture
def blkid_results() -> dict[str, str]:
    return {
        "/dev/sdb1": (
            "DEVNAME=/dev/sdb1\n"
            "TYPE=BitLocker\n"
            "PARTUUID=pu-sdb1\n"
        ),
        "/dev/sdc": (
            "DEVNAME=/dev/sdc\n"
            "TYPE=exfat\n"
            "LABEL=备份盘\n"
            "UUID=u-sdc\n"
        ),
    }
