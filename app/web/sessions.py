"""服务端会话：签名 cookie 存 sid，会话内容保存在内存。"""

from __future__ import annotations

import logging
import secrets
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("session")

COOKIE_NAME = "bsbr_session"
SESSION_TTL = 12 * 3600          # 会话 12 小时
COOKIE_MAX_AGE = SESSION_TTL


class SessionStore:
    def __init__(self, signing_key: bytes) -> None:
        self._signer = URLSafeTimedSerializer(signing_key, salt="bsbr-session")
        self._sessions: dict[str, dict] = {}

    def create(self, username: str) -> str:
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = {
            "username": username,
            "expires": time.time() + SESSION_TTL,
        }
        log.info("用户 %s 登录，会话建立", username)
        return self._signer.dumps({"sid": sid})

    def resolve(self, request) -> dict | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            return None
        try:
            payload = self._signer.loads(raw, max_age=COOKIE_MAX_AGE)
        except (SignatureExpired, BadSignature):
            return None
        session = self._sessions.get(payload.get("sid"))
        if session is None:
            return None
        if session["expires"] < time.time():
            self._sessions.pop(payload["sid"], None)
            return None
        # 滑动续期
        session["expires"] = time.time() + SESSION_TTL
        return session

    def drop(self, request) -> str | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            return None
        try:
            payload = self._signer.loads(raw, max_age=COOKIE_MAX_AGE)
            session = self._sessions.pop(payload["sid"], None)
            if session:
                log.info("用户 %s 退出登录", session["username"])
                return session["username"]
        except (BadSignature, SignatureExpired, KeyError):
            return None
        return None
