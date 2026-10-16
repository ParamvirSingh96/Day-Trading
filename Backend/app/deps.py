from __future__ import annotations

from fastapi import Cookie, Depends
from sqlalchemy.orm import Session

from .auth import get_authenticated_user
from .db import get_db
from .settings import settings


def require_current_user(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    return get_authenticated_user(db, session_token)
