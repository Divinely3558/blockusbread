"""登录限流：窗口内计数、锁定、重置、过期恢复。"""

from __future__ import annotations

from app.web import ratelimit
from app.web.ratelimit import LoginRateLimiter


def test_limiter_allows_below_threshold():
    limiter = LoginRateLimiter(max_failures=3)
    for _ in range(2):
        limiter.failure("1.2.3.4")
    assert limiter.blocked_for("1.2.3.4") == 0


def test_limiter_blocks_at_threshold():
    limiter = LoginRateLimiter(window=600, max_failures=3)
    for _ in range(3):
        limiter.failure("1.2.3.4")
    wait = limiter.blocked_for("1.2.3.4")
    assert wait > 0


def test_limiter_keys_per_ip():
    limiter = LoginRateLimiter(max_failures=2)
    limiter.failure("1.1.1.1")
    limiter.failure("1.1.1.1")
    assert limiter.blocked_for("1.1.1.1") > 0
    assert limiter.blocked_for("2.2.2.2") == 0


def test_limiter_reset_clears():
    limiter = LoginRateLimiter(max_failures=1)
    limiter.failure("9.9.9.9")
    assert limiter.blocked_for("9.9.9.9") > 0
    limiter.reset("9.9.9.9")
    assert limiter.blocked_for("9.9.9.9") == 0


def test_limiter_failures_expire(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["t"])

    limiter = LoginRateLimiter(window=10, max_failures=2)
    limiter.failure("5.5.5.5")
    assert limiter.blocked_for("5.5.5.5") == 0

    clock["t"] += 11
    limiter.failure("5.5.5.5")
    assert limiter.blocked_for("5.5.5.5") == 0


class _FakeRequest:
    def __init__(self, headers=None, host="10.0.0.1"):
        self.headers = headers or {}
        self.client = type("Client", (), {"host": host})()


def test_client_ip_prefers_xff_first_hop():
    req = _FakeRequest({"x-forwarded-for": "203.0.113.7, 172.18.0.2"})
    assert ratelimit.client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_peer():
    assert ratelimit.client_ip(_FakeRequest()) == "10.0.0.1"
