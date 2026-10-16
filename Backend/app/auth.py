from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, UserSession
from .schemas import AuthResponse, AuthUser, LoginRequest, RegisterRequest
from .settings import settings

logger = logging.getLogger("tradehack.auth")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    password_bytes = password.encode("utf-8")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, 120_000)
    return (
        base64.b64encode(salt).decode("utf-8"),
        base64.b64encode(digest).decode("utf-8"),
    )


def _verify_password(password: str, salt_b64: str, password_hash_b64: str) -> bool:
    salt = base64.b64decode(salt_b64.encode("utf-8"))
    _, candidate_hash = _hash_password(password, salt=salt)
    return hmac.compare_digest(candidate_hash, password_hash_b64)


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_auth_response(message: str, user: User) -> AuthResponse:
    return AuthResponse(
        message=message,
        user=AuthUser(name=user.name, email=user.email),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def _create_session(db: Session, user: User, response: Response) -> None:
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.session_max_age_seconds)
    session = UserSession(
        user_id=user.id,
        token_hash=_hash_session_token(raw_token),
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    _set_session_cookie(response, raw_token)


def register_user(db: Session, payload: RegisterRequest, response: Response) -> AuthResponse:
    email = _normalize_email(payload.email)
    existing_user = db.scalar(select(User).where(User.email == email))
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists.",
        )

    salt, password_hash = _hash_password(payload.password)
    user = User(
        name=payload.name.strip(),
        email=email,
        password_salt=salt,
        password_hash=password_hash,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("register email=%s", email)
    return _build_auth_response("Account created successfully.", user)


def login_user(db: Session, payload: LoginRequest, response: Response) -> AuthResponse:
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if not user or not _verify_password(payload.password, user.password_salt, user.password_hash):
        logger.warning("login_failed email=%s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # Invalidate all existing sessions for this user before issuing a new one.
    # This prevents a leaked or stale token from remaining valid after a fresh login.
    db.query(UserSession).filter(UserSession.user_id == user.id).delete()
    db.commit()

    _create_session(db, user, response)
    logger.info("login_success email=%s", email)
    return _build_auth_response("Login successful.", user)


def get_authenticated_user(db: Session, raw_token: str | None) -> User:
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    session = db.scalar(
        select(UserSession).where(UserSession.token_hash == _hash_session_token(raw_token))
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session not found.")

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired.")

    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
    return user


def logout_user(db: Session, raw_token: str | None, response: Response) -> dict[str, str]:
    if raw_token:
        session = db.scalar(
            select(UserSession).where(UserSession.token_hash == _hash_session_token(raw_token))
        )
        if session:
            db.delete(session)
            db.commit()

    clear_session_cookie(response)
    return {"message": "Logout successful."}
