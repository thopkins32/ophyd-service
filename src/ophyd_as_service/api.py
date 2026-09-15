"""Authenticated read-only transports; lifespan owns authentication and devices."""

import asyncio
from collections.abc import Awaitable, Collection
from contextlib import asynccontextmanager
from dataclasses import asdict
from time import time
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Security, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from .auth import Principal
from .auth_profiles import build_authenticator
from .config import ServiceConfig
from .devices import DeviceRegistry, failure
from .errors import ServiceError, auth_error
from .serialization import encode_json
from .subscriptions import (
    WS_MAX_MESSAGE_BYTES,
    WS_SEND_TIMEOUT,
    SubscriptionBroker,
    _close_websocket,
    _CloseSocket,
    _error,
    _text,
)

WS_AUTH_TIMEOUT_SECONDS = 5.0
bearer = HTTPBearer(auto_error=False, scheme_name="JWTAccessToken", bearerFormat="JWT")


class _AuthenticateMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    op: Literal["authenticate"]
    access_token: SecretStr


def _bearer_token(values: list[str]) -> str:
    if len(values) != 1:
        raise auth_error("unauthenticated")
    scheme, token = get_authorization_scheme_param(values[0])
    if scheme.lower() != "bearer" or not token:
        raise auth_error("unauthenticated")
    return token


async def require_reader(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> Principal:
    if credentials is None or len(request.headers.getlist("authorization")) != 1:
        raise auth_error("unauthenticated")
    return await request.app.state.authenticator.require_reader(credentials.credentials)


def _json_response(value: Any, path: str | None = None, *, status: int = 200) -> Response:
    try:
        content = encode_json(value)
    except Exception as exc:
        raise failure("serialization_error", path, exc) from exc
    return Response(content=content, status_code=status, media_type="application/json")


def _service_error_response(error: ServiceError) -> Response:
    response = _json_response(
        {"error": {"code": error.code, "message": error.message, "path": error.path}}, status=error.status
    )
    if error.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


async def _send_handshake(operation: Awaitable[None], *, expires_at: float | None = None) -> None:
    timeout = WS_SEND_TIMEOUT if expires_at is None else min(WS_SEND_TIMEOUT, expires_at - time())
    try:
        await asyncio.wait_for(operation, timeout)
    except TimeoutError as exc:
        if expires_at is not None and expires_at <= time():
            raise _CloseSocket(1008, "token_expired") from exc
        raise _CloseSocket(1013) from exc
    except (WebSocketDisconnect, OSError, RuntimeError) as exc:
        raise _CloseSocket(1013) from exc


async def _deny_websocket(websocket: WebSocket, error: ServiceError) -> None:
    try:
        if websocket.application_state == WebSocketState.CONNECTING:
            if "websocket.http.response" in websocket.scope.get("extensions", {}):
                await _send_handshake(websocket.send_denial_response(_service_error_response(error)))
                return
        elif websocket.application_state == WebSocketState.CONNECTED:
            await _send_handshake(websocket.send_text(_text(_error(error, None))))
    except _CloseSocket:
        pass
    await _close_websocket(websocket, 1013 if error.code == "auth_unavailable" else 1008)


async def _browser_access_token(websocket: WebSocket) -> str:
    await _send_handshake(websocket.accept())
    try:
        message = await asyncio.wait_for(websocket.receive(), WS_AUTH_TIMEOUT_SECONDS)
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason", ""))
        if message.get("bytes") is not None:
            raise _CloseSocket(1003)
        text = message["text"]
        if len(text.encode("utf-8")) > WS_MAX_MESSAGE_BYTES:
            raise _CloseSocket(1009)
        return _AuthenticateMessage.model_validate_json(text).access_token.get_secret_value()
    except (TimeoutError, ValidationError):
        raise auth_error("unauthenticated") from None
    except OSError:
        raise WebSocketDisconnect from None


async def _authenticate_websocket(websocket: WebSocket, allowed_origins: Collection[str]) -> Principal | None:
    if not _same_origin(websocket) and websocket.headers.get("origin") not in allowed_origins:
        await _close_websocket(websocket, 1008)
        return None
    try:
        authorization = websocket.headers.getlist("authorization")
        token = _bearer_token(authorization) if authorization else await _browser_access_token(websocket)
        principal = await websocket.app.state.authenticator.require_reader(token)
        if authorization:
            await _send_handshake(websocket.accept())
        else:
            await _send_handshake(
                websocket.send_text(_text({"type": "authenticated", "expires_at": principal.expires_at})),
                expires_at=principal.expires_at,
            )
        return principal
    except ServiceError as error:
        await _deny_websocket(websocket, error)
    except _CloseSocket as close:
        await _close_websocket(websocket, close.code, close.reason)
    except WebSocketDisconnect:
        pass
    return None


def _same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    try:
        supplied = urlsplit(origin)
        effective = urlsplit(str(websocket.url))
        scheme = {"ws": "http", "wss": "https"}[effective.scheme]
        default_port = 443 if scheme == "https" else 80
        supplied_port, effective_port = supplied.port, effective.port
        return (
            supplied.scheme == scheme
            and supplied.hostname == effective.hostname
            and (default_port if supplied_port is None else supplied_port)
            == (default_port if effective_port is None else effective_port)
            and supplied.username is None
            and supplied.password is None
            and not supplied.path
            and not supplied.query
            and not supplied.fragment
        )
    except ValueError:
        return False


def create_app(config: ServiceConfig) -> FastAPI:
    """Create a single-process application; lifespan constructs/connects roots."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        authenticator = build_authenticator(config.auth)
        app.state.authenticator = authenticator
        registry = DeviceRegistry(config)
        app.state.registry = registry
        broker = SubscriptionBroker(registry)
        app.state.broker = broker
        try:
            await authenticator.start()
            await registry.start()
            yield
        finally:
            registry.stop_accepting()
            errors = []
            for owner in (authenticator, broker, registry):
                try:
                    await owner.close()
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("Service cleanup failed", errors)

    app = FastAPI(title="Read-only Ophyd service", lifespan=lifespan)
    if config.auth.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.auth.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["Authorization"],
        )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return _service_error_response(exc)

    @app.get("/api/v1/devices", dependencies=[Security(require_reader)])
    async def devices(request: Request):
        return _json_response({"devices": list(request.app.state.registry.roots)})

    @app.get("/api/v1/resources/{path:path}", dependencies=[Security(require_reader)])
    async def resource(path: str, request: Request):
        return _json_response(asdict(request.app.state.registry.info(path)), path)

    @app.get("/api/v1/read/{path:path}", dependencies=[Security(require_reader)])
    async def read(path: str, request: Request):
        readings = await request.app.state.registry.read(path)
        return _json_response({"path": path, "readings": readings}, path)

    @app.get("/api/v1/describe/{path:path}", dependencies=[Security(require_reader)])
    async def describe(path: str, request: Request):
        data_keys = await request.app.state.registry.describe(path)
        return _json_response({"path": path, "data_keys": data_keys}, path)

    @app.websocket("/api/v1/ws")
    async def websocket(websocket: WebSocket):
        principal = await _authenticate_websocket(websocket, config.auth.allowed_origins)
        if principal is not None:
            await websocket.app.state.broker.serve(websocket, expires_at=principal.expires_at)

    return app
