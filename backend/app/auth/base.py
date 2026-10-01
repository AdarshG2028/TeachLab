"""Authentication provider interface.

The MVP ships ``LocalAuthProvider`` (development/test only). A Clerk (or any
JWT) provider implements the same interface and is selected via AUTH_PROVIDER.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from starlette.requests import Request


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str = ""
    email: str | None = None


class AuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> Principal:
        """Return the authenticated principal or raise UnauthorizedError."""
