"""Shared native monitors and bounded, latest-state WebSocket delivery."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from functools import partial
from time import time
from typing import Any, Literal

import orjson
from fastapi import WebSocket, WebSocketDisconnect
from ophyd import Signal
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .devices import DeviceRegistry, failure
from .errors import ServiceError
from .serialization import encode_json, snapshot_json

WS_MAX_MESSAGE_BYTES = 65536
WS_SEND_TIMEOUT = 5.0
logger = logging.getLogger(__name__)


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=64)
    op: Literal["subscribe", "unsubscribe"]
    path: str


def _text(value: Any) -> str:
    return encode_json(value).decode("utf-8")


def _error(error: ServiceError, path: str | None, id: str | None = None) -> dict[str, Any]:
    return {"type": "error", "id": id, "path": path, "code": error.code, "message": error.message}


async def _close_websocket(websocket: WebSocket, code: int, reason: str = "") -> None:
    """Bound best-effort closure without interrupting owned cleanup."""
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), WS_SEND_TIMEOUT)
    except (TimeoutError, WebSocketDisconnect, OSError, RuntimeError) as exc:
        logger.warning("Could not deliver WebSocket close %s (%s)", code, type(exc).__name__)


@dataclass(eq=False)
class _Membership:
    session: _Session
    path: str
    source: _Source
    armed: bool = False


@dataclass
class _Control:
    text: str
    sent: asyncio.Future
    arm: _Membership | None


class _CloseSocket(Exception):
    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason


class _Source:
    def __init__(self, broker: SubscriptionBroker, path: str, signal: Any):
        self.broker = broker
        self.path = path
        self.signal = signal
        resource = broker.registry.resource(path)
        self.root = resource.root
        self.classic = resource.info.backend == "ophyd"
        self.loop = broker.registry.loop
        self.members: dict[str, set[_Membership]] = {}
        self.active = True
        self.latest: dict[str, Any] | ServiceError | None = None
        self.frames: dict[str, str] = {}
        self.ready = asyncio.Event()
        self.initial_error: ServiceError | None = None
        self.initialization: asyncio.Task
        self.retirement: asyncio.Task | None = None
        self.removal_error: Exception | None = None
        self.token: int | None = None
        self.registered = False
        self.producer: asyncio.Task | None = None
        self._dirty = False
        self._dirty_lock = threading.Lock()
        self._dirty_event = asyncio.Event()
        self._handoff: dict[str, Any] | ServiceError | None = None
        self._drain_handle: asyncio.Handle | None = None
        # Keep exactly the callable registered with the native object.
        self.callback = self._classic_changed if self.classic else self._async_changed

    async def initialize(self) -> None:
        try:
            if self.classic:
                # This task is broker-owned, never cancelled by a waiting client.
                self.token = await self.broker.registry.classic(
                    self.root,
                    partial(self.signal.subscribe, self.callback, event_type=Signal.SUB_VALUE, run=False),
                    owned=True,
                )
            else:
                # State is complete before this potentially synchronous replay.
                self.signal.subscribe_reading(self.callback)
                self.registered = True
        except Exception as exc:
            self.publish(failure("backend_error", self.path, exc))
            return
        if self.classic and self.active:
            self.producer = asyncio.create_task(self._produce(), name=f"monitor:{self.path}")
            self._classic_changed()

    def _classic_changed(self, *args: Any, **kwargs: Any) -> None:
        # Notifications invalidate a reading; they are not themselves readings.
        # One dirty transition schedules one wake, even during a blocked read.
        with self._dirty_lock:
            if not self.active or self._dirty:
                return
            self._dirty = True
            self.loop.call_soon_threadsafe(self._dirty_event.set)

    async def _produce(self) -> None:
        while self.active:
            await self._dirty_event.wait()
            self._dirty_event.clear()
            with self._dirty_lock:
                if not self.active:
                    return
                self._dirty = False
            try:
                result = await self.broker.registry.read(self.path)
            except ServiceError as exc:
                result = exc
            self.publish(result)

    def _async_changed(self, readings: dict[str, Any]) -> None:
        if not self.active:
            return
        try:
            self._handoff = snapshot_json(readings)
        except Exception as exc:
            self._handoff = failure("serialization_error", self.path, exc)
        if self._drain_handle is None:
            self._drain_handle = self.loop.call_soon(self._drain)

    def _drain(self) -> None:
        self._drain_handle = None
        result, self._handoff = self._handoff, None
        if self.active and result is not None:
            self.publish(result)

    def _encode(self, path: str) -> str:
        if isinstance(self.latest, ServiceError):
            return _text(_error(self.latest, path))
        return _text({"type": "reading", "path": path, "readings": self.latest})

    def frame(self, path: str) -> str:
        if path not in self.frames:
            try:
                self.frames[path] = self._encode(path)
            except Exception as exc:
                self.publish(failure("serialization_error", self.path, exc))
        return self.frames[path]

    def publish(self, result: dict[str, Any] | ServiceError) -> None:
        if not self.active:
            return
        self.latest = result
        self.frames.clear()
        try:
            for path in self.members:
                self.frames[path] = self._encode(path)
        except Exception as exc:
            self.latest = failure("serialization_error", self.path, exc)
            self.frames = {path: self._encode(path) for path in self.members}
        if not self.ready.is_set():
            if isinstance(self.latest, ServiceError):
                self.initial_error = self.latest
            self.ready.set()
        for path, members in self.members.items():
            for member in members:
                if member.armed:
                    member.session.offer(path, self.frames[path])

    def deactivate(self) -> None:
        with self._dirty_lock:
            self.active = False
        self.latest = None
        self.frames.clear()
        self._handoff = None
        if self._drain_handle is not None:
            self._drain_handle.cancel()
            self._drain_handle = None
        if self.producer is not None:
            self.producer.cancel()


class SubscriptionBroker:
    """Application-owned registrations, independent of requesting sockets."""

    def __init__(self, registry: DeviceRegistry):
        self.registry = registry
        self._sources: dict[int, _Source] = {}
        self._sessions: set[_Session] = set()
        self._work: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None

    def own(self, coroutine: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._work.add(task)

        def finished(completed: asyncio.Task) -> None:
            self._work.discard(completed)
            if not completed.cancelled() and (error := completed.exception()) is not None:
                logger.error("Owned subscription work failed", exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(finished)
        return task

    async def serve(self, websocket: WebSocket, *, expires_at: float) -> None:
        expired = expires_at <= time()
        if self._closing or expired:
            code, reason = (1008, "token_expired") if expired else (1013, "")
            await _close_websocket(websocket, code, reason)
            return
        session = _Session(self, websocket, expires_at=expires_at)
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)

    def _check_path(self, path: str) -> None:
        if not self.registry.info(path).monitorable:
            raise ServiceError("not_monitorable", "Only native readable signals can be monitored", path)

    async def subscribe(self, session: _Session, path: str) -> _Membership:
        self._check_path(path)
        async with self.registry.deadline(path):
            signal = await self.registry.resolve(path)
            key = id(signal)
            while (source := self._sources.get(key)) is not None and not source.active:
                if source.removal_error is not None:
                    raise ServiceError(
                        "backend_error",
                        f"{type(source.removal_error).__name__}: {source.removal_error}",
                        path,
                    ) from source.removal_error
                await asyncio.shield(source.retirement)
            if self._closing or session.closed:
                raise asyncio.CancelledError
            if source is None:
                source = _Source(self, path, signal)
                self._sources[key] = source
                source.initialization = self.own(source.initialize(), f"register:{path}")
            member = session.memberships.get(path)
            new = member is None
            if new:
                member = _Membership(session, path, source)
                session.memberships[path] = member
                source.members.setdefault(path, set()).add(member)
            waiting_for_initial = not source.ready.is_set()
            try:
                await asyncio.shield(source.initialization)
                await source.ready.wait()
                error = source.initial_error if waiting_for_initial else None
                if error is None and isinstance(source.latest, ServiceError):
                    error = source.latest
                if error is not None:
                    raise ServiceError(error.code, error.message, path) from error
            except BaseException:
                if new:
                    self.remove(session, path)
                raise
            # Even duplicate replay is armed only after its acknowledgement.
            member.armed = False
            session.pending.pop(path, None)
            return member

    def unsubscribe(self, session: _Session, path: str) -> None:
        # A known inactive path must not construct or connect a lazy component.
        self._check_path(path)
        self.remove(session, path)

    def remove(self, session: _Session, path: str) -> None:
        member = session.memberships.pop(path, None)
        session.pending.pop(path, None)
        if member is None:
            return
        member.armed = False
        source = member.source
        members = source.members[path]
        members.remove(member)
        if not members:
            del source.members[path]
            source.frames.pop(path, None)
        if not source.members:
            source.deactivate()
            source.retirement = self.own(self._retire(source), f"retire:{source.path}")

    async def _retire(self, source: _Source) -> None:
        # Initialization captures a late token even if every client has gone.
        await asyncio.shield(source.initialization)
        if source.producer is not None:
            await asyncio.gather(source.producer, return_exceptions=True)
        try:
            if source.classic and source.token is not None:
                await self.registry.classic(
                    source.root, partial(source.signal.unsubscribe, source.token), owned=True
                )
            elif source.registered:
                source.signal.clear_sub(source.callback)
        except Exception as exc:
            source.removal_error = exc
            failure("backend_error", source.path, exc)
            # Never guess removal succeeded or retry clear_sub (which may create
            # a new native cache). Keep this failed, inactive generation owned.
        else:
            del self._sources[id(source.signal)]

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="subscription-broker-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        sessions = tuple(self._sessions)
        for session in sessions:
            session.stop()
        await asyncio.gather(*(session.done.wait() for session in sessions))
        # These tasks, unlike response waiters and producers, must not be cancelled.
        while self._work:
            await asyncio.gather(*self._work, return_exceptions=True)
        errors = [source.removal_error for source in self._sources.values() if source.removal_error is not None]
        if errors:
            raise ExceptionGroup("Native subscription cleanup failed", errors)


class _Session:
    def __init__(self, broker: SubscriptionBroker, websocket: WebSocket, *, expires_at: float):
        self.broker = broker
        self.websocket = websocket
        self.expires_at = expires_at
        self.memberships: dict[str, _Membership] = {}
        self.pending: OrderedDict[str, str] = OrderedDict()
        self.wakeup = asyncio.Event()
        self.control: _Control | None = None
        self.closed = False
        self.tasks: tuple[asyncio.Task, ...] = ()
        self.done = asyncio.Event()

    def offer(self, path: str, frame: str) -> None:
        if not self.closed:
            # Replacing a path does not move it behind a hotter path.
            self.pending[path] = frame
            self.wakeup.set()

    def stop(self) -> None:
        self.closed = True
        for path in tuple(self.memberships):
            self.broker.remove(self, path)
        self.pending.clear()
        for task in self.tasks:
            task.cancel()
        if self.control is not None and not self.control.sent.done():
            self.control.sent.cancel()

    async def run(self) -> None:
        self.tasks = (
            asyncio.create_task(self._receive(), name="ws-receive"),
            asyncio.create_task(self._send(), name="ws-send"),
            asyncio.create_task(self._wait_for_expiry(), name="ws-expiry"),
        )
        close_code = None
        reason = ""
        try:
            completed, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                if not task.cancelled():
                    task.result()
        except _CloseSocket as exc:
            close_code = exc.code
            reason = exc.reason
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket session failed")
            close_code = 1011
        finally:
            self.stop()
            # An ASGI request may itself be cancelled again during finalization.
            # Its cleanup remains broker-owned and is awaited at shutdown.
            cleanup = self.broker.own(self._finish(close_code, reason), "ws-cleanup")
            await asyncio.shield(cleanup)

    async def _finish(self, close_code: int | None, reason: str = "") -> None:
        try:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if close_code is not None:
                await _close_websocket(self.websocket, close_code, reason)
        finally:
            self.done.set()

    def _check_expiry(self) -> None:
        if self.expires_at <= time():
            raise _CloseSocket(1008, "token_expired")

    async def _wait_for_expiry(self) -> None:
        while (remaining := self.expires_at - time()) > 0:
            await asyncio.sleep(remaining)
        raise _CloseSocket(1008, "token_expired")

    async def _reply(self, value: Any, arm: _Membership | None = None) -> None:
        sent = asyncio.get_running_loop().create_future()
        self.control = _Control(_text(value), sent, arm)
        self.wakeup.set()
        await sent

    async def _send(self) -> None:
        while True:
            if self.control is not None:
                control = self.control
                await self._send_text(control.text)
                self.control = None
                member = control.arm
                if member is not None and self.memberships.get(member.path) is member and member.source.active:
                    member.armed = True
                    self.offer(member.path, member.source.frame(member.path))
                if not control.sent.done():
                    control.sent.set_result(None)
            elif self.pending:
                _, frame = self.pending.popitem(last=False)
                await self._send_text(frame)
            else:
                self.wakeup.clear()
                await self.wakeup.wait()

    async def _send_text(self, text: str) -> None:
        try:
            async with asyncio.timeout(WS_SEND_TIMEOUT):
                self._check_expiry()
                await self.websocket.send_text(text)
        except TimeoutError as exc:
            raise _CloseSocket(1013) from exc

    async def _receive(self) -> None:
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            self._check_expiry()
            if message.get("bytes") is not None:
                raise _CloseSocket(1003)
            text = message["text"]
            if len(text.encode("utf-8")) > WS_MAX_MESSAGE_BYTES:
                raise _CloseSocket(1009)
            data = None
            try:
                data = orjson.loads(text)
                command = _Command.model_validate(data)
            except (orjson.JSONDecodeError, ValidationError) as exc:
                id = path = None
                if isinstance(data, dict):
                    supplied_id, supplied_path = data.get("id"), data.get("path")
                    if isinstance(supplied_id, str) and 1 <= len(supplied_id) <= 64:
                        id = supplied_id
                    if isinstance(supplied_path, str):
                        path = supplied_path
                if isinstance(exc, ValidationError):
                    details = "; ".join(
                        f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                        for item in exc.errors(include_input=False, include_url=False)
                    )
                else:
                    details = "Invalid JSON"
                await self._reply(_error(ServiceError("invalid_request", details), path, id))
                continue
            try:
                if command.op == "subscribe":
                    member = await self.broker.subscribe(self, command.path)
                    await self._reply({"type": "subscribed", "id": command.id, "path": command.path}, member)
                else:
                    self.broker.unsubscribe(self, command.path)
                    await self._reply({"type": "unsubscribed", "id": command.id, "path": command.path})
            except ServiceError as exc:
                await self._reply(_error(exc, command.path, command.id))
