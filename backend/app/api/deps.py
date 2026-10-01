"""FastAPI dependencies: container, authenticated principal, authorized workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.base import Principal
from app.container import Container
from app.core.errors import NotFoundError
from app.db.models import User, Workspace


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_principal(request: Request, c: ContainerDep) -> Principal:
    principal = await c.auth.authenticate(request)
    async with c.db.session() as s:
        await s.execute(pg_insert(User).values(id=principal.user_id, display_name=principal.display_name,
                                               email=principal.email).on_conflict_do_nothing())
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


@dataclass
class WorkspaceContext:
    workspace: Workspace
    principal: Principal

    @property
    def id(self) -> str:
        return self.workspace.id

    @property
    def user_id(self) -> str:
        return self.principal.user_id


async def get_workspace(workspace_id: Annotated[str, Path(max_length=36)], principal: PrincipalDep,
                        c: ContainerDep) -> WorkspaceContext:
    """Authorize access: the workspace must exist AND be owned by the caller.

    Returns 404 (not 403) for other users' workspaces so IDs cannot be probed.
    """
    async with c.db.session() as s:
        ws = await s.get(Workspace, workspace_id)
    if ws is None or ws.owner_id != principal.user_id:
        raise NotFoundError("Workspace not found")
    return WorkspaceContext(ws, principal)


WorkspaceDep = Annotated[WorkspaceContext, Depends(get_workspace)]
