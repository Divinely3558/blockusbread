"""内核 uevent 监听线程：USB 盘插入/拔出时触发一次去抖重扫。"""

from __future__ import annotations

import logging
import socket
import threading

log = logging.getLogger("monitor")

NETLINK_KOBJECT_UEVENT = 15
_RELEVANT_ACTIONS = {"add", "remove", "change"}
_RELEVANT_DEVTYPES = {"disk", "partition"}


class UdevMonitor(threading.Thread):
    """监听 NETLINK_KOBJECT_UEVENT；相关事件去抖后回调（回调在监听线程执行）。"""

    def __init__(self, on_event) -> None:
        super().__init__(name="uevent-monitor", daemon=True)
        self._on_event = on_event
        self._stop = threading.Event()
        self._debounce = threading.Timer(0.0, lambda: None)
        self._debounce.daemon = True
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def trigger(self) -> None:
        """事件到达（或外部主动触发）时，去抖 0.5s 后执行一次重扫。"""
        with self._lock:
            self._debounce.cancel()
            self._debounce = threading.Timer(0.5, self._fire)
            self._debounce.daemon = True
            self._debounce.start()

    def _fire(self) -> None:
        try:
            self._on_event()
        except Exception:
            log.exception("uevent 回调异常")

    def run(self) -> None:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_KOBJECT_UEVENT)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        try:
            # group 1：内核 kobject uevent
            sock.bind((0, 1))
            log.info("uevent 监听已启动")
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                except OSError:
                    break
                if not data:
                    continue
                event = self._parse(data)
                if event is None:
                    continue
                log.debug("uevent: %s", event)
                if (
                    event.get("SUBSYSTEM") == "block"
                    and event.get("ACTION") in _RELEVANT_ACTIONS
                    and event.get("DEVTYPE") in _RELEVANT_DEVTYPES
                ):
                    self.trigger()
        finally:
            sock.close()
            with self._lock:
                self._debounce.cancel()
            log.info("uevent 监听已停止")

    @staticmethod
    def _parse(data: bytes) -> dict[str, str] | None:
        # 报文以 "ACTION@/devices/..." 开头，其余为 NUL 分隔的 KEY=VALUE
        parts = data.split(b"\x00")
        header = parts[0].decode("utf-8", "replace")
        if "@" not in header:
            return None
        action, _, devpath = header.partition("@")
        event = {"ACTION": action, "DEVPATH": devpath}
        for chunk in parts[1:]:
            line = chunk.decode("utf-8", "replace")
            if "=" in line:
                key, _, value = line.partition("=")
                event[key] = value
        return event
