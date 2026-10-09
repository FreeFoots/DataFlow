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
    """演示账号认证；可持久保存令牌哈希，支持服务重启后重新连接。"""

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

    def __init__(self, token_store=None) -> None:
        self._tokens: dict[str, AuthUser] = {}
        self.token_store = token_store

    def login(self, username: str, password: str) -> tuple[str, AuthUser] | None:
        account = self.ACCOUNTS.get(username.strip())
        if not account or not secrets.compare_digest(str(account["password"]), password):
            return None
        token = secrets.token_urlsafe(32)
        user = account["user"]
        if self.token_store:
            self.token_store.save_token(token, user.username, 86400)
        else:
            self._tokens[token] = user
        return token, user

    def authenticate(self, authorization: str | None) -> AuthUser | None:
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        if self.token_store:
            username = self.token_store.token_user(token)
            account = self.ACCOUNTS.get(username or "")
            return account["user"] if account else None
        return self._tokens.get(token)

    def logout(self, authorization: str | None) -> None:
        if not authorization:
            return
        _, _, token = authorization.partition(" ")
        if self.token_store:
            self.token_store.revoke_token(token)
        self._tokens.pop(token, None)
