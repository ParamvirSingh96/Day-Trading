from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import get_authenticated_user
from .db import get_db
from .models import User
from .settings import settings


def require_current_user(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> User:
    return get_authenticated_user(db, session_token)


def require_operator(user: User = Depends(require_current_user)) -> User:
    """Authenticated AND approved to control the shared broker account.

    Logging in is not enough: every account/order/position/autotrade/mode
    route (and the WebSocket feed) requires ``User.is_operator``, which is
    granted only by the local admin command ``scripts/manage_operator.py``
    (``auth.provision_operator``) — never by registration or configuration.
    """
    if not user.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator access required.",
        )
    return user
