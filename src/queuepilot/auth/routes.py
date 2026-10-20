"""Thin HTTP contracts for authentication and user management."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from queuepilot.auth import service
from queuepilot.auth.schemas import (
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserResponse,
    UserStatusUpdate,
)
from queuepilot.core.resources import get_resources
from queuepilot.db.session import get_session

router = APIRouter(prefix="/api/v1", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def auth_error() -> HTTPException:
    return HTTPException(
        401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
    )


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> service.Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise auth_error()
    try:
        return await service.authenticate(
            credentials.credentials,
            request.app.state.settings,
            get_resources(request).sessions,
        )
    except service.AuthError as exc:
        raise auth_error() from exc


@router.post("/auth/login", response_model=TokenPair)
async def login(body: LoginRequest, request: Request, response: Response) -> TokenPair:
    try:
        tokens = await service.login(
            get_resources(request).sessions,
            request.app.state.settings,
            body.email,
            body.password.get_secret_value(),
        )
    except service.AuthError as exc:
        raise auth_error() from exc
    response.headers["Cache-Control"] = "no-store"
    return tokens


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, request: Request, response: Response
) -> TokenPair:
    try:
        tokens = await service.refresh(
            get_resources(request).sessions,
            request.app.state.settings,
            body.refresh_token.get_secret_value(),
        )
    except service.AuthError as exc:
        raise auth_error() from exc
    response.headers["Cache-Control"] = "no-store"
    return tokens


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    principal: Annotated[service.Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str, Header()],
) -> LogoutResponse:
    try:
        await service.logout(
            session,
            authorization.split(" ", 1)[1],
            request.app.state.settings,
            principal,
        )
    except service.AuthError as exc:
        raise auth_error() from exc
    return LogoutResponse()


@router.get("/auth/me", response_model=UserResponse)
async def me(
    principal: Annotated[service.Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    user = await service.get_user(session, principal, principal.id)
    if user is None:
        raise auth_error()
    return user


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    principal: Annotated[service.Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    try:
        return await service.create_user(session, principal, body)
    except service.ForbiddenError as exc:
        raise HTTPException(403, "Forbidden") from exc
    except service.ConflictError as exc:
        raise HTTPException(409, "User already exists") from exc
    except service.AuthError as exc:
        raise HTTPException(422, "Invalid email") from exc


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    principal: Annotated[service.Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    try:
        user = await service.get_user(session, principal, user_id)
    except service.ForbiddenError as exc:
        raise HTTPException(403, "Forbidden") from exc
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.patch("/users/{user_id}/status", response_model=UserResponse)
async def set_status(
    user_id: uuid.UUID,
    body: UserStatusUpdate,
    principal: Annotated[service.Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserResponse:
    try:
        user = await service.set_disabled(session, principal, user_id, body.disabled)
    except service.ForbiddenError as exc:
        raise HTTPException(403, "Forbidden") from exc
    if user is None:
        raise HTTPException(404, "User not found")
    return user
