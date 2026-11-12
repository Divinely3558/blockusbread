"""SFTP 共享信息不得下发口令：口令与网页登录口令同一个，用户手上已有，下发只会多一处泄露面。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.web import api

SECRET = "Sup3r-P@ss*w"


def _request(host: str = "192.168.1.10:8080") -> SimpleNamespace:
    settings = SimpleNamespace(admin_user="admin", admin_password=SECRET)
    return SimpleNamespace(headers={"host": host}, app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


def test_share_info_omits_password() -> None:
    info = asyncio.run(api.share_info(_request()))
    assert "password" not in info
    assert SECRET not in repr(info)
    assert info["username"] == "admin"
    assert info["host"] == "192.168.1.10"
    assert info["uri"] == "sftp://admin@192.168.1.10:2222/"
