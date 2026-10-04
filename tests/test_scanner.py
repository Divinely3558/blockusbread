"""设备扫描：外接盘过滤、superfloppy、GBK 解码、分区号解析。"""

from __future__ import annotations

from app.devices import scanner


def test_decode_utf8_first():
    assert scanner._decode("移动硬盘".encode("utf-8")) == "移动硬盘"


def test_decode_gbk_fallback():
    # Windows 中文卷标常见 GBK：UTF-8 strict 会失败，应回退 GBK
    assert scanner._decode("移动硬盘".encode("gbk")) == "移动硬盘"


def test_decode_invalid_bytes_never_raises():
    result = scanner._decode(b"\xff\xfe\x00ok")
    assert result.endswith("ok")


def test_partition_number():
    assert scanner._partition_number("sdb1") == 1
    assert scanner._partition_number("nvme0n1p2") == 2
    assert scanner._partition_number("sda") == 0


def test_scan_filters_internal_and_handles_superfloppy(
    monkeypatch, lsblk_payload, blkid_results
):
    def fake_run(cmd: list[str], timeout: int = 20) -> str:
        if cmd[0] == "lsblk":
            return lsblk_payload
        if cmd[0] == "blkid":
            return blkid_results.get(cmd[-1], "")
        raise AssertionError(f"未预期的命令：{cmd}")

    monkeypatch.setattr(scanner, "_run", fake_run)
    disks = scanner.scan()

    # 内置 SATA 盘被过滤，只剩两块外接盘
    assert [d.name for d in disks] == ["sdb", "sdc"]

    usb_disk = disks[0]
    assert usb_disk.disk_id == "USB-DISK-001"
    assert usb_disk.removable is True
    part = usb_disk.partitions[0]
    assert part.number == 1
    assert part.bitlocker is True
    assert part.partuuid == "pu-sdb1"

    # superfloppy：整盘文件系统被当作 part1
    stick = disks[1]
    assert len(stick.partitions) == 1
    whole = stick.partitions[0]
    assert whole.number == 1
    assert whole.path == "/dev/sdc"
    assert whole.fstype == "exfat"
    assert whole.label == "备份盘"
    assert whole.uuid == "u-sdc"


def test_scan_hotplug_sata_bridge_treated_as_external(monkeypatch):
    # 部分 SATA 转接硬盘盒 tran 为空，只靠 hotplug 标记识别
    payload = (
        '{"blockdevices": [{"name": "/dev/sdd", "type": "disk", '
        '"tran": null, "rm": false, "hotplug": true, "serial": "BRIDGE1", '
        '"size": 1000, "children": []}]}'
    )
    monkeypatch.setattr(scanner, "_run", lambda cmd, timeout=20: payload)
    disks = scanner.scan()
    assert len(disks) == 1
    assert disks[0].disk_id == "BRIDGE1"
