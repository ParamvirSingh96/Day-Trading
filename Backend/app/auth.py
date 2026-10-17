from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import User, UserSession
from .schemas import AuthResponse, AuthUser, LoginRequest, RegisterRequest
from .settings import settings

logger = logging.getLogger("tradehack.auth")

# ── Password hashing ─────────────────────────────────────────────────────────
# Stored format (versioned):  pbkdf2_sha256$<iterations>$<salt_b64>$<digest_b64>
# Legacy rows (pre-2026-10) hold a bare base64 digest computed with 120,000
# iterations and the salt in the ``password_salt`` column. They still verify
# and are transparently re-hashed at the current cost on the next successful
# login — changing the constant alone would have locked those accounts out.
PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000          # OWASP 2023+ recommendation for PBKDF2-HMAC-SHA256
_LEGACY_ITERATIONS = 120_000


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _hash_password(password: str, salt: bytes | None = None,
                   iterations: int = PBKDF2_ITERATIONS) -> tuple[str, str]:
    """Return ``(salt_b64, encoded_hash)`` in the versioned format."""
    salt = salt or os.urandom(16)
    digest = _pbkdf2(password, salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("utf-8")
    digest_b64 = base64.b64encode(digest).decode("utf-8")
    return salt_b64, f"{PBKDF2_ALGORITHM}${iterations}${salt_b64}${digest_b64}"


def _verify_password(password: str, salt_b64: str, stored: str) -> tuple[bool, bool]:
    """Return ``(matches, needs_rehash)``.

    Accepts both the versioned format and the legacy bare-digest format;
    ``needs_rehash`` is True whenever the stored hash is below the current
    algorithm/iteration count.
    """
    if stored.startswith(f"{PBKDF2_ALGORITHM}$"):
        try:
            _, iters_s, s_b64, d_b64 = stored.split("$", 3)
            iterations = int(iters_s)
            salt = base64.b64decode(s_b64.encode("utf-8"))
        except (ValueError, TypeError):
            return False, False
        candidate = base64.b64encode(_pbkdf2(password, salt, iterations)).decode("utf-8")
        ok = hmac.compare_digest(candidate, d_b64)
        return ok, ok and iterations < PBKDF2_ITERATIONS
    # legacy format
    try:
        salt = base64.b64decode(salt_b64.encode("utf-8"))
    except (ValueError, TypeError):
        return False, False
    candidate = base64.b64encode(_pbkdf2(password, salt, _LEGACY_ITERATIONS)).decode("utf-8")
    ok = hmac.compare_digest(candidate, stored)
    return ok, ok


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _build_auth_response(message: str, user: User) -> AuthResponse:
    return AuthResponse(
        message=message,
        user=AuthUser(name=user.name, email=user.email, is_operator=bool(user.is_operator)),
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


# ── Operator boundary ────────────────────────────────────────────────────────

def _is_invited_operator(email: str) -> bool:
    return email in settings.operator_emails


def _registration_decision(db: Session, email: str) -> tuple[bool, bool]:
    """Return ``(allowed, becomes_operator)`` for a registration attempt.

    * Empty database → the first account is the bootstrap operator.
    * Email listed in OPERATOR_EMAILS → allowed and an operator.
    * REGISTRATION_MODE=open → allowed, but NOT an operator.
    * Otherwise (default ``closed``) → refused.
    """
    user_count = int(db.scalar(select(func.count(User.id))) or 0)
    if user_count == 0:
        return True, True
    if _is_invited_operator(email):
        return True, True
    if settings.registration_mode == "open":
        return True, False
    return False, False


def ensure_operator_bootstrap(db: Session) -> None:
    """Startup reconciliation for databases created before the operator
    boundary existed: promote OPERATOR_EMAILS matches, and if the database
    holds exactly one account with no operator at all, promote it (the
    single-operator deployment this app assumes)."""
    changed = False
    for user in db.scalars(select(User)).all():
        if _is_invited_operator(user.email) and not user.is_operator:
            user.is_operator = True
            changed = True
            logger.info("operator_promoted_by_config user_id=%s", user.id)
    if not db.scalar(select(func.count(User.id)).where(User.is_operator.is_(True))):
        users = db.scalars(select(User)).all()
        if len(users) == 1:
            users[0].is_operator = True
            changed = True
            logger.info("operator_bootstrapped_single_account user_id=%s", users[0].id)
    if changed:
        db.commit()


def register_user(db: Session, payload: RegisterRequest, response: Response) -> AuthResponse:
    email = _normalize_email(payload.email)
    allowed, becomes_operator = _registration_decision(db, email)
    if not allowed:
        logger.warning("register_refused_closed")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed on this instance.",
        )
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
        is_operator=becomes_operator,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("register user_id=%s operator=%s", user.id, becomes_operator)
    return _build_auth_response("Account created successfully.", user)


def login_user(db: Session, payload: LoginRequest, response: Response) -> AuthResponse:
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    ok, needs_rehash = (False, False)
    if user:
        ok, needs_rehash = _verify_password(payload.password, user.password_salt, user.password_hash)
    if not user or not ok:
        logger.warning("login_failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if needs_rehash:
        user.password_salt, user.password_hash = _hash_password(payload.password)
        logger.info("password_rehashed user_id=%s iterations=%d", user.id, PBKDF2_ITERATIONS)
    if _is_invited_operator(email) and not user.is_operator:
        user.is_operator = True
        logger.info("operator_promoted_by_config user_id=%s", user.id)

    # Invalidate all existing sessions for this user before issuing a new one.
    # This prevents a leaked or stale token from remaining valid after a fresh
    # login — and any WebSocket feed bound to those sessions is closed too.
    db.query(UserSession).filter(UserSession.user_id == user.id).delete()
    db.commit()
    _revoke_user_sockets(user.id)

    _create_session(db, user, response)
    logger.info("login_success user_id=%s", user.id)
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
        token_hash = _hash_session_token(raw_token)
        session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
        if session:
            db.delete(session)
            db.commit()
        _revoke_session_sockets(token_hash)

    clear_session_cookie(response)
    return {"message": "Logout successful."}


# ── WebSocket revocation hooks (lazy import: ws depends on models/settings) ──

def _revoke_session_sockets(token_hash: str) -> None:
    try:
        from .ws import revoke_session_sockets
        revoke_session_sockets(token_hash)
    except Exception as exc:  # never let feed cleanup break logout
        logger.debug("ws_revoke_session_skipped error=%s", exc)


def _revoke_user_sockets(user_id: int) -> None:
    try:
        from .ws import revoke_user_sockets
        revoke_user_sockets(user_id)
    except Exception as exc:
        logger.debug("ws_revoke_user_skipped error=%s", exc)
