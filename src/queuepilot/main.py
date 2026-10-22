"""ASGI application factory and process lifecycle."""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.api.health import router as health_router
from queuepilot.api.operations import router as operations_router
from queuepilot.auth.routes import router as auth_router
from queuepilot.core.config import Settings
from queuepilot.core.limits import SlidingWindowLimiter
from queuepilot.core.resources import Resources, create_redis
from queuepilot.db.session import create_engine
from queuepilot.jobs.routes import router as jobs_router
from queuepilot.realtime.hub import Hub
from queuepilot.realtime.routes import router as realtime_router
from queuepilot.tickets.routes import router as tickets_router

logger = logging.getLogger(__name__)


class RootResponse(BaseModel):
    """Public response contract for the root endpoint."""

    message: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Validate config now, acquire lazy clients at startup, close on shutdown."""
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            engine = create_engine(config)
            stack.push_async_callback(engine.dispose)
            redis = create_redis(config)
            stack.push_async_callback(redis.aclose)
            app.state.resources = Resources(
                engine=engine,
                sessions=async_sessionmaker(engine, expire_on_commit=False),
                redis=redis,
                probe_timeout_seconds=config.probe_timeout_seconds,
            )
            hub = Hub(redis)
            hub.start()
            app.state.realtime_hub = hub
            stack.push_async_callback(hub.close)
            logger.info("Starting %s", app.title)
            try:
                yield
            finally:
                del app.state.resources
                del app.state.realtime_hub
                logger.info("Stopped %s", app.title)

    app = FastAPI(title=config.app_name, lifespan=lifespan)
    app.state.settings = config
    app.state.rate_limiter = SlidingWindowLimiter()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.websocket_origins.split(","),
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def request_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        candidate = request.headers.get("X-Request-ID", "")
        request_id = (
            candidate
            if len(candidate) <= 64
            and candidate
            and all(character.isalnum() or character in "-_" for character in candidate)
            else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        if request.url.path.startswith("/api/v1/") and request.method != "OPTIONS":
            client = request.client.host if request.client else "unknown"
            auth_path = request.url.path in {
                "/api/v1/auth/login",
                "/api/v1/auth/refresh",
                "/api/v1/realtime/credentials",
            }
            category = (
                "auth" if auth_path else "read" if request.method == "GET" else "write"
            )
            limit = (
                config.auth_requests_per_minute
                if auth_path
                else config.read_requests_per_minute
                if request.method == "GET"
                else config.write_requests_per_minute
            )
            if not await app.state.rate_limiter.allow(f"{category}:{client}", limit):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Request rate limit exceeded"},
                    headers={
                        "Retry-After": "60",
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    },
                )
        # Bound JSON input before parsing, including chunked requests.
        if request.method in {"POST", "PATCH", "PUT"}:
            length = request.headers.get("content-length")
            if length is not None and length.isdecimal() and int(length) > 16_384:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request too large"},
                    headers={"X-Request-ID": request_id},
                )
            if len(await request.body()) > 16_384:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request too large"},
                    headers={"X-Request-ID": request_id},
                )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(tickets_router)
    app.include_router(jobs_router)
    app.include_router(operations_router)
    app.include_router(realtime_router)

    @app.get("/", response_model=RootResponse)
    async def root() -> RootResponse:
        return RootResponse(message=f"Welcome to {config.app_name}")

    return app
