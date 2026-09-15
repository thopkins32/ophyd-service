import asyncio
import json
from contextlib import contextmanager
from threading import Event

import anyio
import pytest
from ophyd import Signal
from ophyd.sim import EnumSignal
from ophyd_async.core import SoftSignalBackend, StandardReadable, StandardReadableFormat
from starlette.websockets import WebSocketDisconnect

from tests.devices import (
    GatedSignal,
    GuardedAsyncSignalW,
    MutationGuard,
    ObservedAsyncSignalR,
    ObservedSignal,
)
from tests.test_package import call_on_loop, get_json, serving


def _receive(ws):
    """Read one real wire frame without letting a missing reply hang pytest.

    TestClient's public receive methods have no timeout. Bound its outgoing
    ASGI queue wait on the existing portal instead of adding a polling thread
    or inspecting any service state. Only this adapter uses the private queue.
    """

    async def receive_message():
        with anyio.fail_after(5):
            return await ws._send_rx.receive()

    message = ws.portal.call(receive_message)
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason", ""))
    assert message["type"] == "websocket.send", message
    assert isinstance(message.get("text"), str), message
    assert message.get("bytes") is None, message
    return json.loads(message["text"])


def _command(ws, id, op, path):
    ws.send_json({"id": id, "op": op, "path": path})
    return _receive(ws)


def _subscribe(ws, path, id="s"):
    assert _command(ws, id, "subscribe", path) == {
        "type": "subscribed",
        "id": id,
        "path": path,
    }
    frame = _receive(ws)
    assert frame["type"] == "reading", frame
    assert frame["path"] == path, frame
    return frame


def _reading(frame, path, name, value, timestamp=None):
    assert set(frame) == {"type", "path", "readings"}
    assert frame["type"] == "reading"
    assert frame["path"] == path
    assert set(frame["readings"]) == {name}
    reading = frame["readings"][name]
    assert reading["value"] == value
    if timestamp is not None:
        assert reading["timestamp"] == timestamp
    return reading


def _error(frame, id, path, code):
    assert set(frame) == {"type", "id", "path", "code", "message"}
    assert frame["type"] == "error"
    assert frame["id"] == id
    assert frame["path"] == path
    assert frame["code"] == code
    assert isinstance(frame["message"], str)
    assert "Traceback" not in frame["message"]
    return frame


class CountedAsyncSignal(ObservedAsyncSignalR):
    """Observe public reads separately from native synchronous cache replay."""

    def __init__(self, backend, *, name=""):
        self.read_calls = 0
        self.get_calls = 0
        super().__init__(backend, name=name)

    async def read(self, cached=None):
        self.read_calls += 1
        return await super().read(cached=cached)

    async def get_value(self, cached=None):
        self.get_calls += 1
        return await super().get_value(cached=cached)


class MonitoredAsyncDevice(MutationGuard, StandardReadable):
    def __init__(self, name=""):
        backend = SoftSignalBackend(float, 1.234, units="mm", precision=3)
        self.temperature = CountedAsyncSignal(backend)
        self.temperature_setter = backend.set_value
        self.write_only = GuardedAsyncSignalW(SoftSignalBackend(float, 0.0))
        self.add_readables([self.temperature], StandardReadableFormat.HINTED_UNCACHED_SIGNAL)
        super().__init__(name=name)


class IdentityAsyncDevice(MutationGuard, StandardReadable):
    """Two declared aliases and another object intentionally share one name."""

    def __init__(self, name=""):
        first_backend = SoftSignalBackend(float, 1.0)
        other_backend = SoftSignalBackend(float, 10.0)
        self.first = CountedAsyncSignal(first_backend)
        self.alias = self.first
        self.other = CountedAsyncSignal(other_backend)
        self.first_setter = first_backend.set_value
        self.other_setter = other_backend.set_value
        super().__init__(name=name)
        self.first.set_name("same-native-name")
        self.other.set_name("same-native-name")


class MalformedReadingBackend(SoftSignalBackend):
    """An in-memory transport boundary that can deliver unsupported values."""

    def fixture_emit(self, value):
        self.reading = {"value": value, "timestamp": 1234.0, "alarm_severity": 0}
        if self.callback is not None:
            self.callback(self.reading)


class MalformedAsyncDevice(MutationGuard, StandardReadable):
    def __init__(self, name="", invalid_initial=False):
        backend = MalformedReadingBackend(float, 1.0)
        if invalid_initial:
            backend.fixture_emit(1 + 2j)
        self.temperature = CountedAsyncSignal(backend)
        self.fixture_emit = backend.fixture_emit
        super().__init__(name=name)


@pytest.fixture
def monitor_service(tmp_path, entra):
    with serving(
        tmp_path,
        """
[devices.classic]
class = "tests.devices:ClassicDevice"
[devices.async]
class = "tests.test_subscriptions:MonitoredAsyncDevice"
""",
        entra=entra,
    ) as (client, registry):
        classic = registry.roots["classic"]
        asynchronous = registry.roots["async"]
        guarded = [
            classic,
            classic.temperature,
            asynchronous,
            asynchronous.temperature,
            asynchronous.write_only,
        ]
        yield client, registry
    for device in guarded:
        assert device.forbidden_calls == []


def test_shared_monitor_lifecycle_preserves_independent_native_listeners(monitor_service):
    client, registry = monitor_service
    classic = registry.roots["classic"].temperature
    asynchronous = registry.roots["async"]
    async_signal = asynchronous.temperature
    native_values = []
    native_changed = Event()
    async_values = []

    def native_listener(*, value, **kwargs):
        native_values.append(value)
        native_changed.set()

    def async_listener(readings):
        async_values.append(readings[async_signal.name]["value"])

    independent_token = classic.subscribe(native_listener, event_type=Signal.SUB_VALUE, run=False)
    call_on_loop(registry, async_signal.subscribe_reading, async_listener)
    try:
        with client.websocket_connect("/api/v1/ws") as second:
            with client.websocket_connect("/api/v1/ws") as first:
                # A new classic soft Signal has never emitted a callback.
                _reading(
                    _subscribe(first, "classic/temperature"), "classic/temperature", classic.name, 1.0, 1000.0
                )
                initial_async = _subscribe(first, "async/temperature", "async")
                _reading(initial_async, "async/temperature", async_signal.name, 1.234)
                assert initial_async["readings"][async_signal.name]["alarm_severity"] == 0
                assert classic.subscribe_calls == 2
                assert classic.read_calls == 1
                assert async_signal.subscribe_calls == 2
                assert async_signal.read_calls == async_signal.get_calls == 0
                (service_token,) = classic.active_tokens - {independent_token}

                # Both a duplicate and a late client replay the shared latest
                # result, without another registration or native bootstrap read.
                assert _subscribe(first, "classic/temperature", "again")["readings"] == {
                    classic.name: {"value": 1.0, "timestamp": 1000.0},
                }
                _reading(
                    _subscribe(second, "classic/temperature", "late"),
                    "classic/temperature",
                    classic.name,
                    1.0,
                    1000.0,
                )
                assert _subscribe(first, "async/temperature", "async-again") == initial_async
                assert classic.subscribe_calls == 2
                assert classic.read_calls == 1
                assert async_signal.subscribe_calls == 2
                assert _subscribe(second, "async/temperature", "late-async") == initial_async
                assert _command(second, "leave-async", "unsubscribe", "async/temperature") == {
                    "type": "unsubscribed",
                    "id": "leave-async",
                    "path": "async/temperature",
                }
                assert async_signal.clear_calls == 0
                assert async_signal.read_calls == async_signal.get_calls == 0

                for value in (2.0, 3.0):
                    classic.fixture_put(value, timestamp=1000.0 + value)
                    _reading(_receive(first), "classic/temperature", classic.name, value, 1000.0 + value)
                    _reading(_receive(second), "classic/temperature", classic.name, value, 1000.0 + value)
                    call_on_loop(registry, asynchronous.temperature_setter, value + 0.5)
                    _reading(_receive(first), "async/temperature", async_signal.name, value + 0.5)
                assert classic.read_calls == 3
                assert classic.active_tokens == {independent_token, service_token}

            # Closing one multiplexed socket releases only its memberships.
            assert async_signal.cleared.wait(timeout=5)
            assert async_signal.active_callbacks == {async_listener}
            assert classic.active_tokens == {independent_token, service_token}
            classic.fixture_put(4.0, timestamp=1004.0)
            _reading(_receive(second), "classic/temperature", classic.name, 4.0, 1004.0)
            assert _command(second, "last", "unsubscribe", "classic/temperature") == {
                "type": "unsubscribed",
                "id": "last",
                "path": "classic/temperature",
            }
            assert classic.unsubscribed.wait(timeout=5)
            assert classic.unsubscription_tokens == [service_token]
            assert classic.active_tokens == {independent_token}

            native_changed.clear()
            classic.fixture_put(5.0, timestamp=1005.0)
            assert native_changed.wait(timeout=5)
            assert native_values[-1] == 5.0
            call_on_loop(registry, asynchronous.temperature_setter, 5.5)
            assert async_values[-1] == 5.5
            _reading(
                _subscribe(second, "classic/temperature", "new"), "classic/temperature", classic.name, 5.0, 1005.0
            )
            _reading(
                _subscribe(second, "async/temperature", "new-async"), "async/temperature", async_signal.name, 5.5
            )
            assert classic.subscribe_calls == 3
            assert async_signal.subscribe_calls == 3
            assert async_signal.read_calls == async_signal.get_calls == 0
            classic.unsubscribed.clear()
            async_signal.cleared.clear()
        assert classic.unsubscribed.wait(timeout=5)
        assert async_signal.cleared.wait(timeout=5)
        assert classic.active_tokens == {independent_token}
        assert async_signal.active_callbacks == {async_listener}
    finally:
        classic.unsubscribe(independent_token)
        call_on_loop(registry, async_signal.clear_sub, async_listener)


def test_aliases_share_by_object_identity_not_native_name(tmp_path, entra):
    with serving(
        tmp_path,
        """
[devices.identity]
class = "tests.test_subscriptions:IdentityAsyncDevice"
""",
        entra=entra,
    ) as (client, registry):
        device = registry.roots["identity"]
        assert device.first is device.alias
        assert device.first is not device.other
        assert device.first.name == device.other.name == "same-native-name"
        assert device.first.source == device.other.source
        with client.websocket_connect("/api/v1/ws") as ws:
            for path, value in (("first", 1.0), ("alias", 1.0), ("other", 10.0)):
                _reading(_subscribe(ws, f"identity/{path}", path), f"identity/{path}", "same-native-name", value)
            assert device.first.subscribe_calls == 1
            assert device.other.subscribe_calls == 1
            call_on_loop(registry, device.first_setter, 2.0)
            frames = [_receive(ws), _receive(ws)]
            assert {frame["path"] for frame in frames} == {"identity/first", "identity/alias"}
            for frame in frames:
                _reading(frame, frame["path"], "same-native-name", 2.0)
            assert _command(ws, "remove-first", "unsubscribe", "identity/first") == {
                "type": "unsubscribed",
                "id": "remove-first",
                "path": "identity/first",
            }
            call_on_loop(registry, device.first_setter, 3.0)
            _reading(_receive(ws), "identity/alias", "same-native-name", 3.0)
            call_on_loop(registry, device.other_setter, 11.0)
            _reading(_receive(ws), "identity/other", "same-native-name", 11.0)
            assert device.first.clear_calls == 0
            assert device.first.read_calls == device.other.read_calls == 0
        assert device.first.cleared.wait(timeout=5)
        assert device.other.cleared.wait(timeout=5)
        assert device.first.clear_calls == device.other.clear_calls == 1
        assert device.first.active_callbacks == device.other.active_callbacks == set()
    for signal in (device, device.first, device.other):
        assert signal.forbidden_calls == []


def test_classic_enum_monitor_uses_native_read_conversion(tmp_path, monkeypatch, entra):
    with serving(
        tmp_path,
        """
[devices.mode]
class = "ophyd.sim:EnumSignal"
[devices.mode.kwargs]
enum_strings = ["off", "on"]
value = 0
""",
        entra=entra,
    ) as (client, registry):
        signal = registry.roots["mode"]
        attempted = []

        def forbid_mutation(*args, **kwargs):
            attempted.append(True)
            raise AssertionError("Monitoring must not mutate the enum signal")

        # The native class's constructor legitimately seeds this soft signal;
        # guard service-time mutation only, retaining its native read conversion.
        for method in ("put", "set", "stage", "unstage", "trigger", "stop"):
            monkeypatch.setattr(signal, method, forbid_mutation, raising=False)
        with client.websocket_connect("/api/v1/ws") as ws:
            rest = get_json(client, "/api/v1/read/mode")
            assert rest["readings"]["mode"]["value"] == "off"
            assert _subscribe(ws, "mode")["readings"] == rest["readings"]
            EnumSignal.put(signal, 1, timestamp=1001.0)
            _reading(_receive(ws), "mode", "mode", "on", 1001.0)
            assert get_json(client, "/api/v1/read/mode")["readings"]["mode"]["value"] == "on"
        assert attempted == []


def test_inactive_unsubscribe_is_lazy_and_invalid_targets_preserve_membership(monitor_service):
    client, registry = monitor_service
    classic = registry.roots["classic"]
    with client.websocket_connect("/api/v1/ws") as ws:
        _subscribe(ws, "classic/temperature")
        for id in ("lazy", "lazy-again"):
            assert _command(ws, id, "unsubscribe", "classic/unused") == {
                "type": "unsubscribed",
                "id": id,
                "path": "classic/unused",
            }
        assert classic.lazy_constructions == []
        assert not classic.unused_constructed.is_set()
        for operation in ("subscribe", "unsubscribe"):
            for path, code in (
                ("absent", "not_found"),
                ("classic/__class__", "not_found"),
                ("classic", "not_monitorable"),
                ("async/write_only", "not_monitorable"),
            ):
                _error(_command(ws, "bad-target", operation, path), "bad-target", path, code)
        classic.temperature.fixture_put(2.0, timestamp=1002.0)
        _reading(_receive(ws), "classic/temperature", classic.temperature.name, 2.0, 1002.0)


def test_malformed_commands_are_correlated_without_losing_subscriptions(monitor_service):
    client, registry = monitor_service
    path = "classic/temperature"
    malformed = [
        ("{", None, None),
        ("[]", None, None),
        ({"id": "set", "op": "set", "path": path}, "set", path),
        ({"id": "extra", "op": "subscribe", "path": path, "value": 9}, "extra", path),
        ({"id": "missing", "path": path}, "missing", path),
        ({"id": 1, "op": "subscribe", "path": path}, None, path),
        ({"id": "", "op": "subscribe", "path": path}, None, path),
        ({"id": "x" * 65, "op": "subscribe", "path": path}, None, path),
        ({"id": "path", "op": "subscribe", "path": 1}, "path", None),
        ({"id": "op", "op": 1, "path": path}, "op", path),
    ]
    signal = registry.roots["classic"].temperature
    with client.websocket_connect("/api/v1/ws") as ws:
        _subscribe(ws, path, "x" * 64)
        for index, (payload, id, error_path) in enumerate(malformed):
            ws.send_text(payload if isinstance(payload, str) else json.dumps(payload))
            _error(_receive(ws), id, error_path, "invalid_request")
            signal.fixture_put(index + 2, timestamp=1002 + index)
            _reading(_receive(ws), path, signal.name, index + 2, 1002 + index)
        assert signal.subscribe_calls == 1


def test_binary_messages_close_and_release_memberships(monitor_service):
    client, registry = monitor_service
    signal = registry.roots["classic"].temperature
    with client.websocket_connect("/api/v1/ws") as ws:
        _subscribe(ws, "classic/temperature")
        ws.send_bytes(b'{"id":"b","op":"subscribe","path":"classic/temperature"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            _receive(ws)
        assert closed.value.code == 1003
    assert signal.unsubscribed.wait(timeout=5)
    assert signal.active_tokens == set()


def test_message_size_limit_counts_utf8_bytes_and_accepts_exact_boundary(monitor_service):
    client, registry = monitor_service
    signal = registry.roots["classic"].temperature
    with client.websocket_connect("/api/v1/ws") as ws:
        _subscribe(ws, "classic/temperature")
        command = json.dumps({"id": "limit", "op": "unsubscribe", "path": "classic/unused"})
        ws.send_text(command + " " * (65536 - len(command.encode("utf-8"))))
        assert _receive(ws) == {"type": "unsubscribed", "id": "limit", "path": "classic/unused"}
        oversized = json.dumps({"id": "large", "op": "subscribe", "path": "é" * 32768}, ensure_ascii=False)
        assert len(oversized) < 65536 < len(oversized.encode("utf-8"))
        ws.send_text(oversized)
        with pytest.raises(WebSocketDisconnect) as closed:
            _receive(ws)
        assert closed.value.code == 1009
    assert signal.unsubscribed.wait(timeout=5)
    assert signal.active_tokens == set()
    assert registry.roots["classic"].lazy_constructions == []


@pytest.mark.parametrize("origin", [None, "http://testserver"])
def test_native_and_same_origin_clients_are_accepted(monitor_service, origin):
    client, registry = monitor_service
    headers = {} if origin is None else {"origin": origin}
    with client.websocket_connect("/api/v1/ws", headers=headers) as ws:
        _reading(_subscribe(ws, "classic/temperature"), "classic/temperature", "classic_temperature", 1.0, 1000.0)


@pytest.mark.parametrize(
    "origin", ["null", "https://testserver", "http://elsewhere", "http://testserver:8000", "http://testserver:0"]
)
def test_cross_origin_clients_are_rejected_before_accept(monitor_service, origin):
    client, registry = monitor_service
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws", headers={"origin": origin}):
            pytest.fail("Cross-origin connection was accepted")
    assert registry.roots["classic"].temperature.subscribe_calls == 0


@pytest.mark.parametrize("failure", ["backend_error", "serialization_error"])
def test_classic_producer_errors_are_latest_state_and_recover(tmp_path, failure, entra):
    with serving(
        tmp_path,
        """
[devices.source]
class = "tests.devices:GatedSignal"
""",
        entra=entra,
    ) as (client, registry):
        signal = registry.roots["source"]
        with client.websocket_connect("/api/v1/ws") as active:
            _subscribe(active, "source")
            if failure == "backend_error":
                signal.read_error = RuntimeError("native monitor read failed")
                signal.fixture_put(2.0, timestamp=1002.0)
            else:
                signal.fixture_put(1 + 2j, timestamp=1002.0)
            error = _error(_receive(active), None, "source", failure)
            if failure == "backend_error":
                assert "RuntimeError" in error["message"]
                assert "native monitor read failed" in error["message"]
            with client.websocket_connect("/api/v1/ws") as late:
                _error(_command(late, "error-now", "subscribe", "source"), "error-now", "source", failure)
                assert signal.subscribe_calls == 1
                assert signal.read_calls == 2
                signal.read_error = None
                signal.fixture_put(3.0, timestamp=1003.0)
                _reading(_receive(active), "source", "source", 3.0, 1003.0)
                _reading(_subscribe(late, "source", "recovered"), "source", "source", 3.0, 1003.0)
                assert signal.subscribe_calls == 1
                assert signal.read_calls == 3
        assert signal.unsubscribed.wait(timeout=5)
        assert signal.active_tokens == set()
    assert signal.forbidden_calls == []


def test_async_callback_serialization_failure_is_contained_and_recovers(tmp_path, entra):
    with serving(
        tmp_path,
        """
[devices.async]
class = "tests.test_subscriptions:MalformedAsyncDevice"
""",
        entra=entra,
    ) as (client, registry):
        device = registry.roots["async"]
        signal = device.temperature
        with client.websocket_connect("/api/v1/ws") as active:
            _subscribe(active, "async/temperature")
            # This call propagates exceptions from the actual application loop;
            # malformed driver data must not escape into native callback dispatch.
            call_on_loop(registry, device.fixture_emit, 1 + 2j)
            _error(_receive(active), None, "async/temperature", "serialization_error")
            with client.websocket_connect("/api/v1/ws") as late:
                _error(
                    _command(late, "invalid-now", "subscribe", "async/temperature"),
                    "invalid-now",
                    "async/temperature",
                    "serialization_error",
                )
                assert signal.subscribe_calls == 1
                call_on_loop(registry, device.fixture_emit, 2.0)
                _reading(_receive(active), "async/temperature", signal.name, 2.0, 1234.0)
                _reading(
                    _subscribe(late, "async/temperature", "valid-now"),
                    "async/temperature",
                    signal.name,
                    2.0,
                    1234.0,
                )
                assert signal.subscribe_calls == 1
                assert signal.read_calls == signal.get_calls == 0
        assert signal.cleared.wait(timeout=5)
        assert signal.active_callbacks == set()
    assert device.forbidden_calls == signal.forbidden_calls == []


def test_bad_initial_async_replay_fails_correlated_and_releases_callback(tmp_path, entra):
    with serving(
        tmp_path,
        """
read_timeout = 30.0
[devices.async]
class = "tests.test_subscriptions:MalformedAsyncDevice"
[devices.async.kwargs]
invalid_initial = true
""",
        entra=entra,
    ) as (client, registry):
        device = registry.roots["async"]
        signal = device.temperature
        with client.websocket_connect("/api/v1/ws") as ws:
            _error(
                _command(ws, "bad-seed", "subscribe", "async/temperature"),
                "bad-seed",
                "async/temperature",
                "serialization_error",
            )
            assert signal.cleared.wait(timeout=5)
            assert signal.active_callbacks == set()
            call_on_loop(registry, device.fixture_emit, 4.0)
            _reading(
                _subscribe(ws, "async/temperature", "new-seed"), "async/temperature", signal.name, 4.0, 1234.0
            )
            assert signal.subscribe_calls == 2
            assert signal.read_calls == signal.get_calls == 0
    assert device.forbidden_calls == signal.forbidden_calls == []


class RaceSoftSignal(ObservedAsyncSignalR):
    def __init__(self, *, name="", value=0.0):
        self.fixture_backend = SoftSignalBackend(float, value)
        super().__init__(self.fixture_backend, name=name)

    def fixture_put(self, value):
        self.fixture_backend.set_value(value)


class RaceSilentBackend(SoftSignalBackend):
    """A connected native backend whose monitor has not produced a value yet."""

    def set_callback(self, callback):
        if callback is None:
            super().set_callback(None)
        else:
            assert self.callback is None
            self.callback = callback


class RaceSilentSignal(ObservedAsyncSignalR):
    def __init__(self, *, name=""):
        super().__init__(RaceSilentBackend(float, 1.0), name=name)


class RaceRemovalFailureSignal(RaceSoftSignal):
    def __init__(self, **kwargs):
        self.clear_attempted = Event()
        self.removal_error = RuntimeError("fixture native monitor removal rejected")
        super().__init__(**kwargs)

    def clear_sub(self, function):
        self.clear_calls += 1
        self.clear_attempted.set()
        raise self.removal_error


class RaceLateSignal(GatedSignal):
    def subscribe(self, callback, event_type=None, run=True):
        self.subscribe_finished.clear()
        token = ObservedSignal.subscribe(self, callback, event_type=event_type, run=run)
        self.subscribe_started.set()
        try:
            assert self.subscribe_release.wait(timeout=10), "fixture registration gate was not released"
            return token
        finally:
            self.subscribe_finished.set()


class RaceSnapshotSignal(GatedSignal):
    """Hold a completed native snapshot before returning it to the service."""

    def __init__(self, **kwargs):
        self.subscribed_while_reading = False
        super().__init__(**kwargs)

    def read(self):
        with self._observation_lock:
            self.read_calls += 1
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
        self.read_finished.clear()
        try:
            reading = Signal.read(self)
            self.read_started.set()
            assert self.read_release.wait(timeout=10), "fixture read gate was not released"
            return reading
        finally:
            with self._observation_lock:
                self.active_reads -= 1
            self.read_finished.set()

    def subscribe(self, callback, event_type=None, run=True):
        with self._observation_lock:
            self.subscribed_while_reading |= self.active_reads > 0
        return super().subscribe(callback, event_type=event_type, run=run)


class RaceSendGate:
    """Gate one real ASGI send for the socket identified by ?slow=1."""

    def __init__(self):
        self.entered = Event()
        self.cancelled = Event()
        self.receive_cancelled = Event()
        self.application_done = Event()
        self.release = asyncio.Event()
        self.path = None

    def arm(self, path):
        self.path = path
        self.entered.clear()
        self.release.clear()

    async def send(self, message, send):
        if message["type"] == "websocket.send" and "text" in message:
            frame = json.loads(message["text"])
            if frame.get("type") == "reading" and frame.get("path") == self.path:
                self.path = None
                self.entered.set()
                try:
                    await asyncio.wait_for(self.release.wait(), timeout=10)
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
        await send(message)


class RaceSendMiddleware:
    def __init__(self, app, *, gate):
        self.app = app
        self.gate = gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket" or scope.get("query_string") != b"slow=1":
            await self.app(scope, receive, send)
            return

        async def gated_send(message):
            await self.gate.send(message, send)

        async def observed_receive():
            try:
                return await receive()
            except asyncio.CancelledError:
                self.gate.receive_cancelled.set()
                raise

        try:
            await self.app(scope, observed_receive, gated_send)
        finally:
            self.gate.application_done.set()


@contextmanager
def _race_serving(tmp_path, text, gate, *, entra):
    from fastapi.testclient import TestClient

    from ophyd_as_service.api import create_app
    from ophyd_as_service.config import load_config

    config = tmp_path / "race-devices.toml"
    config.write_text(entra.toml + text)
    app = create_app(load_config(config))
    app.add_middleware(RaceSendMiddleware, gate=gate)
    with TestClient(app, headers=entra.headers) as client:
        yield client, app.state.registry


def _race_assert_reading(frame, path, value):
    assert frame["type"] == "reading", frame
    assert frame["path"] == path, frame
    assert frame["readings"][path]["value"] == value, frame


def test_race_slow_socket_coalesces_latest_without_starving_pending_paths(tmp_path, entra):
    gate = RaceSendGate()
    with _race_serving(
        tmp_path,
        """
[devices.hot]
class = "tests.test_subscriptions:RaceSoftSignal"
[devices.other]
class = "tests.test_subscriptions:RaceSoftSignal"
""",
        gate,
        entra=entra,
    ) as (client, registry):
        hot, other = registry.roots["hot"], registry.roots["other"]
        with client.websocket_connect("/api/v1/ws?slow=1") as slow:
            with client.websocket_connect("/api/v1/ws") as fast:
                for socket in (slow, fast):
                    _race_assert_reading(_subscribe(socket, "hot"), "hot", 0.0)
                    _race_assert_reading(_subscribe(socket, "other"), "other", 0.0)
                try:
                    call_on_loop(registry, gate.arm, "hot")
                    call_on_loop(registry, hot.fixture_put, 1.0)
                    assert gate.entered.wait(timeout=5)
                    _race_assert_reading(_receive(fast), "hot", 1.0)

                    # The fast subscriber proves each source result has reached
                    # publication before the next notification is emitted.
                    call_on_loop(registry, hot.fixture_put, 2.0)
                    _race_assert_reading(_receive(fast), "hot", 2.0)
                    call_on_loop(registry, other.fixture_put, 11.0)
                    _race_assert_reading(_receive(fast), "other", 11.0)
                    for value in (3.0, 4.0):
                        call_on_loop(registry, hot.fixture_put, value)
                        _race_assert_reading(_receive(fast), "hot", value)

                    call_on_loop(registry, gate.release.set)
                    _race_assert_reading(_receive(slow), "hot", 1.0)
                    # Replacing hot's pending value must preserve its position,
                    # not append behind other or retain the 2/3 backlog.
                    _race_assert_reading(_receive(slow), "hot", 4.0)
                    _race_assert_reading(_receive(slow), "other", 11.0)
                    assert _command(slow, "u", "unsubscribe", "hot") == {
                        "type": "unsubscribed",
                        "id": "u",
                        "path": "hot",
                    }
                finally:
                    call_on_loop(registry, gate.release.set)
        assert hot.cleared.wait(timeout=5)
        assert other.cleared.wait(timeout=5)
        assert hot.active_callbacks == other.active_callbacks == set()


def test_race_unsubscribe_ack_is_barrier_after_an_inflight_frame(tmp_path, entra):
    gate = RaceSendGate()
    with _race_serving(
        tmp_path,
        """
[devices.hot]
class = "tests.test_subscriptions:RaceSoftSignal"
[devices.other]
class = "tests.test_subscriptions:RaceSoftSignal"
""",
        gate,
        entra=entra,
    ) as (client, registry):
        hot, other = registry.roots["hot"], registry.roots["other"]
        with client.websocket_connect("/api/v1/ws?slow=1") as slow:
            with client.websocket_connect("/api/v1/ws") as fast:
                _subscribe(slow, "hot")
                _subscribe(slow, "other")
                _subscribe(fast, "other")
                try:
                    call_on_loop(registry, gate.arm, "hot")
                    call_on_loop(registry, hot.fixture_put, 1.0)
                    assert gate.entered.wait(timeout=5)
                    call_on_loop(registry, hot.fixture_put, 2.0)
                    call_on_loop(registry, other.fixture_put, 12.0)
                    _race_assert_reading(_receive(fast), "other", 12.0)
                    slow.send_json({"id": "u", "op": "unsubscribe", "path": "hot"})
                    # Sole native listener removal proves the command ran while
                    # its acknowledgement was waiting behind the gated send.
                    assert hot.cleared.wait(timeout=5)
                    call_on_loop(registry, hot.fixture_put, 3.0)
                    call_on_loop(registry, gate.release.set)
                    _race_assert_reading(_receive(slow), "hot", 1.0)
                    assert _receive(slow) == {
                        "type": "unsubscribed",
                        "id": "u",
                        "path": "hot",
                    }
                    _race_assert_reading(_receive(slow), "other", 12.0)
                    # A further acknowledged round trip exposes any stale hot
                    # frame following the barrier without a timing-based peek.
                    _race_assert_reading(_subscribe(slow, "other", id="again"), "other", 12.0)
                    assert hot.active_callbacks == set()
                finally:
                    call_on_loop(registry, gate.release.set)


@pytest.mark.parametrize("first_exit", ["disconnect", "timeout"])
def test_race_late_classic_registration_is_retired_before_reattach(tmp_path, first_exit, entra):
    with serving(
        tmp_path,
        """
read_timeout = 0.15
[devices.late]
class = "tests.test_subscriptions:RaceLateSignal"
""",
        entra=entra,
    ) as (client, registry):
        signal = registry.roots["late"]
        signal.subscribe_release.clear()
        try:
            with client.websocket_connect("/api/v1/ws") as first:
                first.send_json({"id": "old", "op": "subscribe", "path": "late"})
                assert signal.subscribe_started.wait(timeout=5)
                (old_token,) = signal.subscription_tokens
                if first_exit == "timeout":
                    error = _receive(first)
                    assert (error["type"], error["id"], error["path"], error["code"]) == (
                        "error",
                        "old",
                        "late",
                        "timeout",
                    )
            # The first socket is gone, but its native registration still has
            # not returned its token. A replacement must not register over it.
            with client.websocket_connect("/api/v1/ws") as replacement:
                error = _command(replacement, "waiting", "subscribe", "late")
                assert (error["type"], error["id"], error["path"], error["code"]) == (
                    "error",
                    "waiting",
                    "late",
                    "timeout",
                )
                assert signal.subscribe_calls == 1
                assert signal.active_tokens == {old_token}
                assert signal.unsubscription_tokens == []
                assert signal.destroy_calls == 0
                signal.fixture_put(9.0, timestamp=1009.0)
                signal.subscribe_release.set()
                assert signal.unsubscribed.wait(timeout=5)
                assert signal.unsubscription_tokens == [old_token]
                assert signal.active_tokens == set()
                _race_assert_reading(_subscribe(replacement, "late", id="new"), "late", 9.0)
                new_token = signal.subscription_tokens[-1]
                assert new_token != old_token
                signal.fixture_put(10.0, timestamp=1010.0)
                _race_assert_reading(_receive(replacement), "late", 10.0)
                signal.unsubscribed.clear()
                assert _command(replacement, "done", "unsubscribe", "late") == {
                    "type": "unsubscribed",
                    "id": "done",
                    "path": "late",
                }
                assert signal.unsubscribed.wait(timeout=5)
        finally:
            signal.subscribe_release.set()
            signal.read_release.set()
    assert signal.unsubscription_tokens == [old_token, new_token]
    assert signal.active_tokens == set()
    assert signal.destroy_calls == 1


def test_race_old_classic_read_cannot_publish_into_replacement_generation(tmp_path, entra):
    with serving(
        tmp_path,
        """
read_timeout = 0.15
[devices.source]
class = "tests.test_subscriptions:RaceSnapshotSignal"
""",
        entra=entra,
    ) as (client, registry):
        signal = registry.roots["source"]
        try:
            with client.websocket_connect("/api/v1/ws") as first:
                _race_assert_reading(_subscribe(first, "source"), "source", 1.0)
                signal.read_started.clear()
                signal.read_finished.clear()
                signal.read_release.clear()
                signal.fixture_put(2.0, timestamp=1002.0)
                assert signal.read_started.wait(timeout=5)
            with client.websocket_connect("/api/v1/ws") as replacement:
                error = _command(replacement, "blocked", "subscribe", "source")
                assert (error["type"], error["id"], error["path"], error["code"]) == (
                    "error",
                    "blocked",
                    "source",
                    "timeout",
                )
                assert signal.active_reads == 1
                assert signal.subscribe_calls == 1
                assert signal.destroy_calls == 0
                assert not signal.subscribed_while_reading
                signal.fixture_put(9.0, timestamp=1009.0)
                signal.read_release.set()
                assert signal.read_finished.wait(timeout=5)
                assert signal.unsubscribed.wait(timeout=5)
                frame = _subscribe(replacement, "source", id="new")
                _race_assert_reading(frame, "source", 9.0)
                assert frame["readings"]["source"]["timestamp"] == 1009.0
                signal.fixture_put(10.0, timestamp=1010.0)
                _race_assert_reading(_receive(replacement), "source", 10.0)
                assert signal.max_active_reads == 1
        finally:
            signal.read_release.set()
            signal.subscribe_release.set()
    assert not signal.destroyed_while_reading
    assert not signal.subscribed_while_reading
    assert signal.active_tokens == set()
    assert signal.unsubscription_tokens == signal.subscription_tokens


def test_race_first_async_monitor_without_native_result_times_out_and_releases(tmp_path, entra):
    with serving(
        tmp_path,
        """
read_timeout = 0.05
[devices.silent]
class = "tests.test_subscriptions:RaceSilentSignal"
[devices.healthy]
class = "tests.test_subscriptions:RaceSoftSignal"
""",
        entra=entra,
    ) as (client, registry):
        silent = registry.roots["silent"]
        with client.websocket_connect("/api/v1/ws") as socket:
            error = _command(socket, "no-result", "subscribe", "silent")
            assert (error["type"], error["id"], error["path"], error["code"]) == (
                "error",
                "no-result",
                "silent",
                "timeout",
            )
            assert silent.cleared.wait(timeout=5)
            assert silent.active_callbacks == set()
            _race_assert_reading(_subscribe(socket, "healthy"), "healthy", 0.0)
            call_on_loop(registry, registry.roots["healthy"].fixture_put, 7.0)
            _race_assert_reading(_receive(socket), "healthy", 7.0)


def test_race_failed_native_removal_rejects_reattach_and_shutdown_cleans_other_roots(tmp_path, caplog, entra):
    with pytest.raises(ExceptionGroup) as shutdown:
        with serving(
            tmp_path,
            """
[devices.bad]
class = "tests.test_subscriptions:RaceRemovalFailureSignal"
[devices.good]
class = "tests.devices:ObservedSignal"
""",
            entra=entra,
        ) as (client, registry):
            bad, good = registry.roots["bad"], registry.roots["good"]
            with client.websocket_connect("/api/v1/ws") as socket:
                _subscribe(socket, "bad")
                _subscribe(socket, "good")
                assert _command(socket, "remove", "unsubscribe", "bad") == {
                    "type": "unsubscribed",
                    "id": "remove",
                    "path": "bad",
                }
                assert bad.clear_attempted.wait(timeout=5)
                error = _command(socket, "reattach", "subscribe", "bad")
                assert (error["type"], error["id"], error["path"], error["code"]) == (
                    "error",
                    "reattach",
                    "bad",
                    "backend_error",
                )
                assert bad.subscribe_calls == 1
                assert bad.clear_calls == 1
                assert len(bad.active_callbacks) == 1
                # The failed native callback still exists, but is inactive and
                # must not resume delivery to the removed socket membership.
                call_on_loop(registry, bad.fixture_put, 42.0)
                _race_assert_reading(_subscribe(socket, "good", id="still-alive"), "good", 1.0)
            assert good.unsubscribed.wait(timeout=5)
    assert bad.subscribe_calls == 1
    assert bad.clear_calls == 1
    assert good.active_tokens == set()
    assert good.destroyed.is_set()

    def reported_errors(error):
        yield error
        for child in getattr(error, "exceptions", ()):
            yield from reported_errors(child)
        if error.__cause__ is not None:
            yield from reported_errors(error.__cause__)

    assert any(error is bad.removal_error for error in reported_errors(shutdown.value))
    assert any(
        record.exc_info and any(error is bad.removal_error for error in reported_errors(record.exc_info[1]))
        for record in caplog.records
    )


def test_race_send_timeout_closes_socket_and_finishes_its_peer_and_native_monitor(tmp_path, monkeypatch, entra):
    from starlette.websockets import WebSocketDisconnect

    import ophyd_as_service.subscriptions as subscriptions

    monkeypatch.setattr(subscriptions, "WS_SEND_TIMEOUT", 0.05)
    gate = RaceSendGate()
    with _race_serving(
        tmp_path,
        """
[devices.source]
class = "tests.test_subscriptions:RaceSoftSignal"
""",
        gate,
        entra=entra,
    ) as (client, registry):
        signal = registry.roots["source"]
        with client.websocket_connect("/api/v1/ws?slow=1") as socket:
            try:
                call_on_loop(registry, gate.arm, "source")
                assert _command(socket, "start", "subscribe", "source") == {
                    "type": "subscribed",
                    "id": "start",
                    "path": "source",
                }
                assert gate.entered.wait(timeout=5)
                with pytest.raises(WebSocketDisconnect) as closed:
                    _receive(socket)
                assert closed.value.code == 1013
                assert gate.cancelled.wait(timeout=5)
                assert gate.receive_cancelled.wait(timeout=5)
                assert signal.cleared.wait(timeout=5)
                # Completion while the client context is still open requires
                # the endpoint to have settled its blocked receive peer too.
                assert gate.application_done.wait(timeout=5)
                assert signal.active_callbacks == set()
            finally:
                call_on_loop(registry, gate.release.set)
