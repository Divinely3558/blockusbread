"""登录失败限流：按来源 IP 的滑动窗口计数（单进程内存态，重启即清除）。"""

from __future__ import annotations

import time

WINDOW_SECONDS = 600      # 统计窗口：10 分钟
MAX_FAILURES = 5          # 窗口内最多失败 5 次，第 6 次起拒绝


class LoginRateLimiter:
    def __init__(
        self,
        window: int = WINDOW_SECONDS,
        max_failures: int = MAX_FAILURES,
    ) -> None:
        self._window = window
        self._max = max_failures
        self._fails: dict[str, list[float]] = {}

    def blocked_for(self, ip: str) -> int:
        """该 IP 当前还需等待多少秒才可再次尝试；0 表示放行。"""
        now = time.monotonic()
        recent = [t for t in self._fails.get(ip, []) if now - t < self._window]
        if ip in self._fails:
            self._fails[ip] = recent
        if len(recent) < self._max:
            return 0
        return int(self._window - (now - recent[0])) + 1

    def failure(self, ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._fails.get(ip, []) if now - t < self._window]
        recent.append(now)
        self._fails[ip] = recent

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


def client_ip(request) -> str:
    """来源 IP：优先取反代写入的 X-Forwarded-For 首跳，否则用直连地址。

    反代（tls profile 的 Caddy）与应用同容器网络，可信；
    直连场景下该头即便伪造，影响的也只是限流计数本身。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff.strip():
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
