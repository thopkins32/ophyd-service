"""Read-only HTTP surface; importing or creating the app does not own devices."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

from .config import ServiceConfig
from .devices import DeviceRegistry, ServiceError, failure
from .serialization import encode_json
from .subscriptions import SubscriptionBroker


def _json_response(value: Any, path: str | None = None, *, status: int = 200) -> Response:
    try:
        content = encode_json(value)
    except Exception as exc:
        raise failure("serialization_error", path, exc) from exc
    return Response(content=content, status_code=status, media_type="application/json")


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
        registry = DeviceRegistry(config)
        app.state.registry = registry
        await registry.start()
        broker = SubscriptionBroker(registry)
        app.state.broker = broker
        try:
            yield
        finally:
            registry.stop_accepting()
            errors = []
            for owner in (broker, registry):
                try:
                    await owner.close()
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("Service cleanup failed", errors)

    app = FastAPI(title="Read-only Ophyd service", lifespan=lifespan)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return _json_response(
            {"error": {"code": exc.code, "message": exc.message, "path": exc.path}}, status=exc.status
        )

    @app.get("/api/v1/devices")
    async def devices(request: Request):
        return _json_response({"devices": list(request.app.state.registry.roots)})

    @app.get("/api/v1/resources/{path:path}")
    async def resource(path: str, request: Request):
        return _json_response(asdict(request.app.state.registry.info(path)), path)

    @app.get("/api/v1/read/{path:path}")
    async def read(path: str, request: Request):
        readings = await request.app.state.registry.read(path)
        return _json_response({"path": path, "readings": readings}, path)

    @app.get("/api/v1/describe/{path:path}")
    async def describe(path: str, request: Request):
        data_keys = await request.app.state.registry.describe(path)
        return _json_response({"path": path, "data_keys": data_keys}, path)

    @app.websocket("/api/v1/ws")
    async def websocket(websocket: WebSocket):
        if not _same_origin(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.app.state.broker.serve(websocket)

    return app
