"""Local development auth: identity from the ``X-User-Id`` header.

This trusts the caller and therefore must never run in production; Settings
validation refuses AUTH_PROVIDER=local when ENV=production. Authorization
(workspace ownership) is still enforced server-side for every request.
"""

from __future__ import annotations

import re

from starlette.requests import Request

from app.auth.base import AuthProvider, Principal
from app.core.errors import UnauthorizedError

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")


class LocalAuthProvider(AuthProvider):
    def __init__(self, default_user_id: str) -> None:
        self.default_user_id = default_user_id

    async def authenticate(self, request: Request) -> Principal:
        user_id = request.headers.get("x-user-id") or request.query_params.get("user_id") or self.default_user_id
        if not _USER_ID_RE.match(user_id):
            raise UnauthorizedError("Invalid user id")
        return Principal(user_id=user_id, display_name=request.headers.get("x-user-name", user_id))
