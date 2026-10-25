"""Resolve the Bearer API key that every protected route requires."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection

from ..auth.service import authenticate_api_key
from .db import database


_bearer = HTTPBearer(auto_error=False)


def require_key(
    connection: Annotated[Connection, Depends(database)],
    bearer: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer),
    ] = None,
) -> str:
    """The id of the presented API key. Anything else is a plain 401."""
    key = (authenticate_api_key(connection, bearer.credentials)
           if bearer is not None else None)
    if key is None:
        raise HTTPException(
            401, {"code": "not_authenticated"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key.id


consumer_key = require_key
