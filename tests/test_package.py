import asyncio
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager

import numpy as np
import pytest
from fastapi.testclient import TestClient
from ophyd import Component, Device
from ophyd_async.core import SignalR, SoftSignalBackend

from ophyd_as_service.api import create_app
from ophyd_as_service.config import load_config
from tests.devices import MutationGuard, ObservedAsyncSignalR, ObservedSignal


@contextmanager
def serving(tmp_path, text):
    path = tmp_path / "devices.toml"
    path.write_text(text)
    with TestClient(create_app(load_config(path))) as client:
        yield client, client.app.state.registry


def call_on_loop(registry, function, *args):
    result = Future()

    def call():
        try:
            result.set_result(function(*args))
        except Exception as exc:
            result.set_exception(exc)

    registry.loop.call_soon_threadsafe(call)
    return result.result(timeout=5)


def await_on_loop(registry, coroutine):
    return asyncio.run_coroutine_threadsafe(coroutine, registry.loop).result(timeout=5)


def get_json(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def assert_error(response, status, code, path):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["path"] == path
    assert "Traceback" not in error["message"]
    return error


@pytest.fixture
def paired_service(tmp_path, monkeypatch):
    stage_attempts = []

    def forbid_stage(signal, *args, **kwargs):
        stage_attempts.append(signal.name)
        raise AssertionError("Read-only requests must not stage or unstage signals")

    # The factory-produced SignalR deliberately has no per-instance guard.
    monkeypatch.setattr(SignalR, "stage", forbid_stage)
    monkeypatch.setattr(SignalR, "unstage", forbid_stage)
    with serving(
        tmp_path,
        """
[devices.classic]
class = "tests.devices:ClassicDevice"
[devices.async]
class = "tests.devices:AsyncDevice"
""",
    ) as (client, registry):
        classic = registry.roots["classic"]
        asynchronous = registry.roots["async"]
        guarded = [classic, classic.temperature, asynchronous, asynchronous.write_only]
        yield client, registry
    assert stage_attempts == []
    for device in guarded:
        assert device.forbidden_calls == []


def test_reads_preserve_native_values_timestamps_and_descriptions(paired_service):
    client, registry = paired_service
    classic = registry.roots["classic"]
    asynchronous = registry.roots["async"]

    assert get_json(client, "/api/v1/devices") == {"devices": ["classic", "async"]}
    classic_reading = {"classic_temperature": {"value": 1.0, "timestamp": 1000.0}}
    assert get_json(client, "/api/v1/read/classic/temperature") == {
        "path": "classic/temperature",
        "readings": classic_reading,
    }
    assert get_json(client, "/api/v1/read/classic") == {
        "path": "classic",
        "readings": classic_reading,
    }
    classic_description = get_json(client, "/api/v1/describe/classic/temperature")
    assert classic_description == {
        "path": "classic/temperature",
        "data_keys": classic.temperature.describe(),
    }
    assert classic_description["data_keys"]["classic_temperature"] == {
        "source": "SIM:classic_temperature",
        "dtype": "number",
        "shape": [],
    }

    native_async = await_on_loop(registry, asynchronous.temperature.read(cached=False))
    assert native_async["async-temperature"]["value"] == 1.234
    assert native_async["async-temperature"]["timestamp"] > 0
    assert native_async["async-temperature"]["alarm_severity"] == 0
    assert get_json(client, "/api/v1/read/async/temperature") == {
        "path": "async/temperature",
        "readings": native_async,
    }
    assert get_json(client, "/api/v1/read/async") == {"path": "async", "readings": native_async}
    async_description = get_json(client, "/api/v1/describe/async/temperature")
    assert async_description == {
        "path": "async/temperature",
        "data_keys": await_on_loop(registry, asynchronous.temperature.describe()),
    }
    metadata = async_description["data_keys"]["async-temperature"]
    assert metadata["source"] == "soft://async-temperature"
    assert metadata["shape"] == []
    assert metadata["units"] == "mm"
    assert metadata["precision"] == 3

    classic.temperature.fixture_put(2.5, timestamp=1001.0)
    call_on_loop(registry, asynchronous.temperature_setter, 5.678)
    updated_async = await_on_loop(registry, asynchronous.temperature.read(cached=False))
    assert get_json(client, "/api/v1/read/classic/temperature")["readings"] == {
        "classic_temperature": {"value": 2.5, "timestamp": 1001.0},
    }
    assert updated_async["async-temperature"]["value"] == 5.678
    assert get_json(client, "/api/v1/read/async/temperature")["readings"] == updated_async
    assert get_json(client, "/api/v1/describe/classic/temperature") == classic_description
    assert get_json(client, "/api/v1/describe/async/temperature") == async_description


def test_rejected_operations_do_not_mutate_devices(paired_service):
    client, registry = paired_service
    before_classic = get_json(client, "/api/v1/read/classic/temperature")
    before_async = get_json(client, "/api/v1/read/async/temperature")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = client.request(method, "/api/v1/read/classic/temperature", json={"value": 99})
        assert response.status_code == 405
    assert client.post("/api/v1/read/async/temperature", json={"value": 99}).status_code == 405
    assert client.post("/api/v1/set/classic/temperature", json={"value": 99}).status_code == 404
    assert_error(client.get("/api/v1/read/classic/__class__"), 404, "not_found", "classic/__class__")
    assert_error(client.get("/api/v1/read/async/temperature_setter"), 404, "not_found", "async/temperature_setter")
    assert get_json(client, "/api/v1/resources/async/write_only") == {
        "path": "async/write_only",
        "backend": "ophyd-async",
        "readable": False,
        "monitorable": False,
        "children": [],
    }
    assert_error(client.get("/api/v1/read/async/write_only"), 409, "not_readable", "async/write_only")
    assert_error(client.get("/api/v1/describe/async/write_only"), 409, "not_readable", "async/write_only")
    assert get_json(client, "/api/v1/read/classic/temperature") == before_classic
    assert get_json(client, "/api/v1/read/async/temperature") == before_async
    assert registry.roots["async"].write_only.forbidden_calls == []


def test_catalog_is_lazy_and_literal_paths_do_not_expand_native_reads(paired_service):
    client, registry = paired_service
    classic = registry.roots["classic"]
    assert get_json(client, "/api/v1/resources/classic") == {
        "path": "classic",
        "backend": "ophyd",
        "readable": True,
        "monitorable": False,
        "children": ["classic/temperature", "classic/unused"],
    }
    assert get_json(client, "/api/v1/resources/classic/unused") == {
        "path": "classic/unused",
        "backend": "ophyd",
        "readable": True,
        "monitorable": True,
        "children": [],
    }
    assert set(get_json(client, "/api/v1/read/classic")["readings"]) == {"classic_temperature"}
    assert classic.lazy_constructions == []
    assert not classic.unused_constructed.is_set()
    assert get_json(client, "/api/v1/read/classic/unused")["readings"]["classic_unused"]["value"] == 9.0
    assert classic.lazy_constructions == ["unused"]
    assert classic.unused.forbidden_calls == []
    assert set(get_json(client, "/api/v1/read/classic")["readings"]) == {"classic_temperature"}

    assert get_json(client, "/api/v1/resources/async/labels")["children"] == [
        "async/labels/a.b",
        "async/labels/a~0~1b",
    ]
    dotted = get_json(client, "/api/v1/read/async/labels/a.b")
    escaped = get_json(client, "/api/v1/read/async/labels/a~0~1b")
    assert dotted == {
        "path": "async/labels/a.b",
        "readings": await_on_loop(registry, registry.roots["async"].labels["a.b"].read(cached=False)),
    }
    assert next(iter(dotted["readings"].values()))["value"] == 1
    assert escaped == {
        "path": "async/labels/a~0~1b",
        "readings": await_on_loop(registry, registry.roots["async"].labels["a~/b"].read(cached=False)),
    }
    assert next(iter(escaped["readings"].values()))["value"] == 2
    assert_error(client.get("/api/v1/read/async/labels/a/b"), 404, "not_found", "async/labels/a/b")
    assert_error(
        client.get("/api/v1/read/async/labels/a/b/__class__"), 404, "not_found", "async/labels/a/b/__class__"
    )


def test_sparse_vectors_and_declared_private_children_are_discoverable(tmp_path):
    with serving(
        tmp_path,
        """
[devices.everything]
class = "ophyd_async.testing:ParentOfEverythingDevice"
""",
    ) as (client, registry):
        root = registry.roots["everything"]
        children = get_json(client, "/api/v1/resources/everything")["children"]
        assert "everything/_sig_rw" in children
        assert get_json(client, "/api/v1/resources/everything/vector")["children"] == [
            "everything/vector/1",
            "everything/vector/3",
        ]
        assert get_json(client, "/api/v1/read/everything/_sig_rw") == {
            "path": "everything/_sig_rw",
            "readings": await_on_loop(registry, root._sig_rw.read(cached=False)),
        }
        assert (
            get_json(client, "/api/v1/read/everything/vector/3/a_int")["readings"]["everything-vector-3-a_int"][
                "value"
            ]
            == 1
        )
        assert_error(client.get("/api/v1/resources/everything/vector/0"), 404, "not_found", "everything/vector/0")
        assert_error(client.get("/api/v1/resources/everything/vector/2"), 404, "not_found", "everything/vector/2")
        assert_error(client.get("/api/v1/read/everything/parent"), 404, "not_found", "everything/parent")
        assert_error(client.get("/api/v1/read/everything"), 409, "not_readable", "everything")


def test_sim_motor_root_and_leaf_share_native_name_without_path_collision(tmp_path):
    with serving(
        tmp_path,
        """
[devices.axis]
class = "ophyd_async.sim:SimMotor"
[devices.axis.kwargs]
initial_value = 2.5
instant = true
units = "mm"
""",
    ) as (client, registry):
        motor = registry.roots["axis"]
        native = await_on_loop(registry, motor.user_readback.read(cached=False))
        assert native["axis"]["value"] == 2.5
        assert get_json(client, "/api/v1/read/axis") == {"path": "axis", "readings": native}
        assert get_json(client, "/api/v1/read/axis/user_readback") == {
            "path": "axis/user_readback",
            "readings": native,
        }
        description = get_json(client, "/api/v1/describe/axis/user_readback")["data_keys"]["axis"]
        assert description["source"] == "soft://axis"
        assert description["units"] == "mm"
        assert description["shape"] == []
        assert "axis/user_readback" in get_json(client, "/api/v1/resources/axis")["children"]
        assert get_json(client, "/api/v1/resources/axis/user_readback")["monitorable"] is True
        assert await_on_loop(registry, motor.user_setpoint.get_value()) == 2.5


def test_native_config_only_device_stays_empty_and_array_enum_table_leaves_preserve_shape(tmp_path):
    from tests.test_subscriptions import _subscribe

    with serving(
        tmp_path,
        """
[devices.data]
class = "ophyd_async.testing:OneOfEverythingDevice"
""",
    ) as (client, registry):
        assert get_json(client, "/api/v1/read/data") == {"path": "data", "readings": {}}
        assert get_json(client, "/api/v1/describe/data") == {"path": "data", "data_keys": {}}
        array_readings = get_json(client, "/api/v1/read/data/ndarray")["readings"]
        assert array_readings["data-ndarray"]["value"] == [
            [1, 2, 3],
            [4, 5, 6],
        ]
        array_key = get_json(client, "/api/v1/describe/data/ndarray")["data_keys"]["data-ndarray"]
        assert array_key["dtype"] == "array"
        assert array_key["shape"] == [2, 3]
        enum_readings = get_json(client, "/api/v1/read/data/a_enum")["readings"]
        assert enum_readings["data-a_enum"]["value"] == "Bbb"
        enum_key = get_json(client, "/api/v1/describe/data/a_enum")["data_keys"]["data-a_enum"]
        assert enum_key["choices"] == ["Aaa", "Bbb", "Ccc"]
        table_readings = get_json(client, "/api/v1/read/data/table")["readings"]
        assert table_readings["data-table"]["value"] == {
            "a_bool": [False, False, True, True],
            "a_int": [1, 8, -9, 32],
            "a_float": [1.8, 8.2, -6.0, 32.9887],
            "a_str": ["Hello", "World", "Foo", "Bar"],
            "a_enum": ["Aaa", "Bbb", "Aaa", "Ccc"],
        }
        table_key = get_json(client, "/api/v1/describe/data/table")["data_keys"]["data-table"]
        assert table_key["shape"] == [4]
        native_key = await_on_loop(registry, registry.roots["data"].table.describe())["data-table"]
        assert table_key["source"] == native_key["source"]
        assert table_key["dtype"] == native_key["dtype"]
        assert table_key["dtype_numpy"] == [list(column) for column in native_key["dtype_numpy"]]
        with client.websocket_connect("/api/v1/ws") as ws:
            for path, readings in (
                ("data/ndarray", array_readings),
                ("data/a_enum", enum_readings),
                ("data/table", table_readings),
            ):
                assert _subscribe(ws, path, id=path) == {
                    "type": "reading",
                    "path": path,
                    "readings": readings,
                }


class EdgeValuesSignal(ObservedSignal):
    """A soft native reading with awkward but supported JSON values."""

    def __init__(self, *, name=""):
        foreign_dtype = ">i4" if sys.byteorder == "little" else "<i4"
        self.matrix = np.array([[1, 99, 2, 99, 3, 99], [4, 99, 5, 99, 6, 99]], dtype=foreign_dtype)[:, ::2]
        self.payload = {
            "matrix": self.matrix,
            "unsigned": np.array([2**64 - 1], dtype=np.uint64),
            "scalar_integer": np.uint64(2**64 - 1),
            "nonfinite": np.array([[np.nan, np.inf, -np.inf], [1.5, 0, -2.5]]),
            "scalar_float": np.float32(np.nan),
            "strings": np.array([["a", "b"], ["c", "d"]]),
        }
        super().__init__(name=name, value=self.payload, timestamp=1000.0)


def test_wire_normalization_preserves_integer_precision_and_does_not_mutate_native_arrays(tmp_path):
    from tests.test_subscriptions import _receive, _subscribe

    with serving(
        tmp_path,
        """
[devices.edges]
class = "tests.test_package:EdgeValuesSignal"
""",
    ) as (client, registry):
        signal = registry.roots["edges"]
        original_bytes = signal.matrix.tobytes()
        original_strides = signal.matrix.strides
        assert not signal.matrix.flags.c_contiguous
        assert not signal.matrix.dtype.isnative
        response = client.get("/api/v1/read/edges")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "path": "edges",
            "readings": {
                "edges": {
                    "timestamp": 1000.0,
                    "value": {
                        "matrix": [[1, 2, 3], [4, 5, 6]],
                        "unsigned": [2**64 - 1],
                        "scalar_integer": 2**64 - 1,
                        "nonfinite": [[None, None, None], [1.5, 0, -2.5]],
                        "scalar_float": None,
                        "strings": [["a", "b"], ["c", "d"]],
                    },
                }
            },
        }
        assert b"18446744073709551615" in response.content
        with client.websocket_connect("/api/v1/ws") as ws:
            assert _subscribe(ws, "edges") == {"type": "reading", **response.json()}
            assert signal.matrix.tobytes() == original_bytes
            assert signal.matrix.strides == original_strides
            assert not signal.matrix.dtype.isnative
            assert np.isnan(signal.payload["nonfinite"][0, 0])
            assert np.isposinf(signal.payload["nonfinite"][0, 1])
            assert np.isneginf(signal.payload["nonfinite"][0, 2])
            signal.fixture_put(1 + 2j, timestamp=1001.0)
            assert_error(client.get("/api/v1/read/edges"), 500, "serialization_error", "edges")
            error = _receive(ws)
            assert error["type"] == "error"
            assert error["id"] is None
            assert error["path"] == "edges"
            assert error["code"] == "serialization_error"
            signal.fixture_put(7.5, timestamp=1002.0)
            recovered = get_json(client, "/api/v1/read/edges")
            assert recovered["readings"] == {
                "edges": {"value": 7.5, "timestamp": 1002.0},
            }
            assert _receive(ws) == {"type": "reading", **recovered}
        assert signal.forbidden_calls == []


class MutableReadingSignal(ObservedAsyncSignalR):
    """Expose a test-owned soft backend whose notification buffer can be reused."""

    def __init__(self, *, name=""):
        self.fixture_backend = SoftSignalBackend(np.ndarray, np.array([[1, 2], [3, 4]]))
        super().__init__(backend=self.fixture_backend, name=name)


def test_async_callback_handoff_owns_mutable_array_and_reading_metadata(tmp_path):
    from tests.test_subscriptions import _receive, _subscribe

    with serving(
        tmp_path,
        """
[devices.handoff]
class = "tests.test_package:MutableReadingSignal"
""",
    ) as (client, registry):
        signal = registry.roots["handoff"]
        with client.websocket_connect("/api/v1/ws") as ws:
            initial = _subscribe(ws, "handoff")
            assert initial["readings"]["handoff"]["value"] == [[1, 2], [3, 4]]

            def emit_then_reuse_buffer():
                signal.fixture_backend.set_value(np.array([[5, 6], [7, 8]]))
                reading = signal.fixture_backend.reading
                timestamp = reading["timestamp"]
                # Native dispatch has returned, but this synchronous loop call
                # cannot yield to the broker's deferred drain before mutation.
                reading["value"].fill(-1)
                reading["timestamp"] = 1000.0
                reading["alarm_severity"] = 2
                return timestamp

            timestamp = call_on_loop(registry, emit_then_reuse_buffer)
            assert _receive(ws) == {
                "type": "reading",
                "path": "handoff",
                "readings": {
                    "handoff": {
                        "value": [[5, 6], [7, 8]],
                        "timestamp": timestamp,
                        "alarm_severity": 0,
                    },
                },
            }
            assert get_json(client, "/api/v1/read/handoff")["readings"] == {
                "handoff": {
                    "value": [[-1, -1], [-1, -1]],
                    "timestamp": 1000.0,
                    "alarm_severity": 2,
                },
            }
            call_on_loop(registry, signal.fixture_backend.set_value, np.array([1 + 2j]))
            error = _receive(ws)
            assert error["type"] == "error"
            assert error["id"] is None
            assert error["path"] == "handoff"
            assert error["code"] == "serialization_error"
    assert signal.forbidden_calls == []


def test_direct_async_read_fetches_silent_getter_change_instead_of_monitor_cache(tmp_path):
    from tests.test_subscriptions import _receive, _subscribe

    with serving(
        tmp_path,
        """
[devices.state]
class = "tests.devices:StateBackedAsyncDevice"
""",
    ) as (client, registry):
        root = registry.roots["state"]
        signal = root.temperature
        with client.websocket_connect("/api/v1/ws") as ws:
            initial = _subscribe(ws, "state/temperature")
            assert initial["readings"]["state-temperature"]["value"] == 1.234
            assert root.getter_calls == 0
            call_on_loop(registry, setattr, root, "state_value", 5.678)
            assert _subscribe(ws, "state/temperature", id="cached") == initial
            assert root.getter_calls == 0
            fresh = get_json(client, "/api/v1/read/state/temperature")
            reading = fresh["readings"]["state-temperature"]
            assert reading["value"] == 5.678
            assert reading["alarm_severity"] == 0
            assert reading["timestamp"] >= initial["readings"]["state-temperature"]["timestamp"]
            assert _receive(ws) == {"type": "reading", **fresh}
            assert root.getter_calls == 1
    assert signal.active_callbacks == set()
    assert root.forbidden_calls == []
    assert signal.forbidden_calls == []


def test_classic_timeout_keeps_native_work_serialized_while_async_root_remains_usable(tmp_path):
    from tests.test_subscriptions import _receive, _subscribe

    with serving(
        tmp_path,
        """
read_timeout = 0.2
[devices.blocked]
class = "tests.devices:GatedSignal"
[devices.async]
class = "tests.devices:AsyncDevice"
""",
    ) as (client, registry):
        blocked = registry.roots["blocked"]
        blocked.read_release.clear()
        with ThreadPoolExecutor(max_workers=2) as requests, client.websocket_connect("/api/v1/ws") as ws:
            try:
                _subscribe(ws, "async/temperature")
                first = requests.submit(client.get, "/api/v1/read/blocked")
                assert blocked.read_started.wait(timeout=5)
                other = requests.submit(client.get, "/api/v1/read/async/temperature").result(timeout=5)
                assert other.status_code == 200, other.text
                assert other.json()["readings"]["async-temperature"]["value"] == 1.234
                call_on_loop(registry, registry.roots["async"].temperature_setter, 4.5)
                assert _receive(ws)["readings"]["async-temperature"]["value"] == 4.5
                assert_error(first.result(timeout=5), 504, "timeout", "blocked")
                second = requests.submit(client.get, "/api/v1/read/blocked").result(timeout=5)
                assert_error(second, 504, "timeout", "blocked")
                assert blocked.read_calls == 1
                assert blocked.active_reads == 1
                assert blocked.max_active_reads == 1
                assert not blocked.destroyed.is_set()
            finally:
                blocked.read_release.set()
            assert blocked.read_finished.wait(timeout=5)
            assert get_json(client, "/api/v1/read/blocked") == {
                "path": "blocked",
                "readings": {"blocked": {"value": 1.0, "timestamp": 1000.0}},
            }
            assert registry.roots["blocked"] is blocked
            assert blocked.max_active_reads == 1
    assert blocked.destroyed.is_set()
    assert not blocked.destroyed_while_reading


def test_native_read_error_keeps_type_message_and_can_recover(tmp_path):
    with serving(
        tmp_path,
        """
[devices.failing]
class = "tests.devices:GatedSignal"
[devices.failing.kwargs]
fail_read = "native fixture read failed"
""",
    ) as (client, registry):
        error = assert_error(client.get("/api/v1/read/failing"), 502, "backend_error", "failing")
        assert "RuntimeError" in error["message"]
        assert "native fixture read failed" in error["message"]
        registry.roots["failing"].read_error = None
        assert get_json(client, "/api/v1/read/failing")["readings"] == {
            "failing": {"value": 1.0, "timestamp": 1000.0},
        }


class ConstructorFailureSignal(ObservedSignal):
    def __init__(self, **kwargs):
        raise ValueError("native lazy construction failed")


class ConnectionFailureSignal(ObservedSignal):
    def wait_for_connection(self, timeout=0.0):
        raise ConnectionError("native lazy connection failed")


class FailingLazyDevice(MutationGuard, Device):
    healthy = Component(ObservedSignal, value=1.0, timestamp=1000.0)
    construct_failure = Component(ConstructorFailureSignal, lazy=True, kind="omitted")
    connect_failure = Component(ConnectionFailureSignal, lazy=True, kind="omitted")


@pytest.mark.parametrize(
    "operation, child, error_type, message",
    [
        ("read", "construct_failure", "ValueError", "native lazy construction failed"),
        ("describe", "connect_failure", "ConnectionError", "native lazy connection failed"),
    ],
)
def test_known_lazy_failures_are_backend_errors_not_missing_resources(
    tmp_path, operation, child, error_type, message
):
    with serving(
        tmp_path,
        """
[devices.lazy]
class = "tests.test_package:FailingLazyDevice"
""",
    ) as (client, registry):
        path = f"lazy/{child}"
        assert path in get_json(client, "/api/v1/resources/lazy")["children"]
        assert get_json(client, f"/api/v1/resources/{path}")["readable"] is True
        error = assert_error(client.get(f"/api/v1/{operation}/{path}"), 502, "backend_error", path)
        assert error_type in error["message"]
        assert message in error["message"]
        assert get_json(client, "/api/v1/read/lazy/healthy")["readings"] == {
            "lazy_healthy": {"value": 1.0, "timestamp": 1000.0},
        }
        assert registry.roots["lazy"].forbidden_calls == []
