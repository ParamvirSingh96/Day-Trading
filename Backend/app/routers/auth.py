from fastapi import APIRouter, Body, Cookie, Depends, Request, Response
from sqlalchemy.orm import Session

from ..auth import get_authenticated_user, login_user, logout_user, register_user
from ..db import get_db
from ..limiter import limiter
from ..schemas import AuthResponse, AuthUser, LoginRequest, RegisterRequest
from ..settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse)
@limiter.limit("5/minute")
def register(
    request: Request,
    response: Response,
    payload: RegisterRequest = Body(...),
    db: Session = Depends(get_db),
) -> AuthResponse:
    return register_user(db, payload, response)


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def login(
    request: Request,
    response: Response,
    payload: LoginRequest = Body(...),
    db: Session = Depends(get_db),
) -> AuthResponse:
    return login_user(db, payload, response)


@router.get("/me", response_model=AuthResponse)
def me(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> AuthResponse:
    user = get_authenticated_user(db, session_token)
    return AuthResponse(message="Authenticated.", user=AuthUser(name=user.name, email=user.email))


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, str]:
    return logout_user(db, session_token, response)
