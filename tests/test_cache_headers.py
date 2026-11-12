"""登录态接口必须 no-store，静态资源与入口页必须 no-cache，两者不能混用。"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main


def _client() -> TestClient:
    # 不起 lifespan（本机无权读写 /etc/ssh/sshd_config），只塞一个总能判定未登录的会话桩
    main.app.state.sessions = SimpleNamespace(resolve=lambda request: None)
    return TestClient(main.app)


def test_api_responses_are_no_store() -> None:
    client = _client()
    resp = client.get("/api/volumes")
    assert resp.status_code == 401          # 未登录也要带上响应头，缓存不该只挑成功响应
    assert resp.headers["cache-control"] == "no-store"


def test_static_and_index_are_no_cache() -> None:
    client = _client()
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
