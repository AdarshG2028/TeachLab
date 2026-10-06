"""No-sign-in identity: a random client id generated and kept by the browser.

There is no account. The browser generates a UUID (122 random bits) and sends it as ``X-Client-Id``
(``client_id`` query parameter for EventSource/downloads). The server never issues it, so workspaces are private
to whoever holds the id: it cannot be guessed or enumerated, and there is no listing of other users' ids.
Treat the id like a password: losing it means losing access to the workspaces; sharing it shares them.
"""

from __future__ import annotations

import re

from starlette.requests import Request

from app.auth.base import AuthProvider, Principal
from app.core.errors import UnauthorizedError

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class AnonymousAuthProvider(AuthProvider):
    async def authenticate(self, request: Request) -> Principal:
        cid = (request.headers.get("x-client-id") or request.query_params.get("client_id") or "").lower()
        if not _UUID_RE.match(cid):
            raise UnauthorizedError("Missing or invalid client id")
        return Principal(user_id=f"anon-{cid}", display_name="Guest")
