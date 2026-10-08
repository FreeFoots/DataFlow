from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    username: str
    display_name: str
    role: str

    def public(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


class AuthService:
    """进程内账号认证；令牌在服务重启后失效。"""

    ACCOUNTS = {
        "admin": {
            "password": "admin123",
            "user": AuthUser("demo_admin", "admin", "系统管理员", "admin"),
        },
        "growth": {
            "password": "growth123",
            "user": AuthUser("demo_growth_ops", "growth", "用户增长运营", "growth_ops"),
        },
        "channel": {
            "password": "channel123",
            "user": AuthUser("demo_channel_ops", "channel", "渠道投放运营", "channel_ops"),
        },
        "content": {
            "password": "content123",
            "user": AuthUser("demo_content_ops", "content", "内容运营", "content_ops"),
        },
    }

    def __init__(self) -> None:
        self._tokens: dict[str, AuthUser] = {}

    def login(self, username: str, password: str) -> tuple[str, AuthUser] | None:
        account = self.ACCOUNTS.get(username.strip())
        if not account or not secrets.compare_digest(str(account["password"]), password):
            return None
        token = secrets.token_urlsafe(32)
        user = account["user"]
        self._tokens[token] = user
        return token, user

    def authenticate(self, authorization: str | None) -> AuthUser | None:
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        return self._tokens.get(token)

    def logout(self, authorization: str | None) -> None:
        if not authorization:
            return
        _, _, token = authorization.partition(" ")
        self._tokens.pop(token, None)
