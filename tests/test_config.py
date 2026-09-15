import asyncio
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ophyd import Signal
from ophyd_async.core import Device as AsyncDevice
from ophyd_async.core import DeviceConnector, SignalR, soft_signal_r_and_setter
from pydantic import ValidationError

from ophyd_as_service.api import create_app
from ophyd_as_service.config import ServiceConfig, load_config
from ophyd_as_service.devices import DeviceRegistry
from ophyd_as_service.errors import ServiceError


def test_parse_complete_config_before_import(tmp_path, monkeypatch, entra):
    path = tmp_path / "devices.toml"
    path.write_text(
        entra.toml
        + """
    [devices.classic]
    class = "uninstalled_driver:Device"
    [devices.classic.kwargs]
    prefix = "SIM:"
    channels = [1, 3]
    [devices.async]
    class = "another_driver:Device"
    """
    )

    def unexpected_import(name):
        pytest.fail(f"configuration parsing imported {name}")

    monkeypatch.setattr(importlib, "import_module", unexpected_import)
    config = load_config(path)
    assert list(config.devices) == ["classic", "async"]
    assert config.devices["classic"].kwargs == {"prefix": "SIM:", "channels": [1, 3]}


def test_explicit_empty_devices(tmp_path, entra):
    path = tmp_path / "empty.toml"
    path.write_text(entra.toml + "[devices]\n")
    config = load_config(path)
    assert config.devices == {}
    assert config.auth.allowed_origins == []
    with TestClient(create_app(config), headers=entra.headers) as client:
        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        assert response.json() == {"devices": []}


def test_inline_auth_preserves_root_timeouts(tmp_path, entra):
    path = tmp_path / "timeouts.toml"
    path.write_text(entra.toml + "connect_timeout = 0.125\nread_timeout = 0.25\n[devices]\n")
    config = load_config(path)
    assert config.connect_timeout == 0.125
    assert config.read_timeout == 0.25


@pytest.mark.parametrize(
    ("auth_text", "location"),
    [
        pytest.param("", ("auth",), id="missing-auth"),
        pytest.param("[auth]\n", ("auth", "profile"), id="missing-profile"),
        pytest.param(
            '[auth.profile]\ntenant_id = "{tenant_id}"\napi_client_id = "{api_client_id}"\n',
            ("auth", "profile", "type"),
            id="missing-type",
        ),
        pytest.param(
            '[auth.profile]\ntype = "generic"\ntenant_id = "{tenant_id}"\napi_client_id = "{api_client_id}"\n',
            ("auth", "profile", "type"),
            id="generic-not-supported",
        ),
        pytest.param(
            '[auth.profile]\ntype = "oidc"\ntenant_id = "{tenant_id}"\napi_client_id = "{api_client_id}"\n',
            ("auth", "profile", "type"),
            id="other-provider-not-supported",
        ),
        pytest.param(
            '[auth]\ntype = "entra"\ntenant_id = "{tenant_id}"\napi_client_id = "{api_client_id}"\n',
            ("auth", "type"),
            id="flat-profile-not-supported",
        ),
        pytest.param(
            '[auth]\n[profile]\ntype = "entra"\ntenant_id = "{tenant_id}"\napi_client_id = "{api_client_id}"\n',
            ("profile",),
            id="misplaced-root-profile",
        ),
    ],
)
def test_auth_requires_explicit_nested_entra_profile(tmp_path, monkeypatch, entra, auth_text, location):
    path = tmp_path / "invalid-auth.toml"
    path.write_text(
        auth_text.format(tenant_id=entra.tenant_id, api_client_id=entra.api_client_id)
        + '[devices.root]\nclass = "uninstalled_driver:Device"\n'
    )

    def unexpected_import(name):
        pytest.fail(f"invalid authentication configuration imported {name}")

    monkeypatch.setattr(importlib, "import_module", unexpected_import)
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, ValidationError)
    assert location in {entry["loc"] for entry in error.value.__cause__.errors()}


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("auth", "disable", "true"),
        ("auth", "client_secret", '"not-a-secret"'),
        ("auth", "issuer", '"https://issuer.example.test"'),
        ("auth", "import_path", '"arbitrary_module:Profile"'),
        ("auth.profile", "client_secret", '"not-a-secret"'),
        ("auth.profile", "allowed_origins", "[]"),
    ],
)
def test_auth_rejects_unknown_options(tmp_path, entra, table, field, value):
    path = tmp_path / "unknown-auth-option.toml"
    option = f"{field} = {value}\n"
    path.write_text(
        "[auth]\n"
        + (option if table == "auth" else "")
        + '[auth.profile]\ntype = "entra"\n'
        + f'tenant_id = "{entra.tenant_id}"\napi_client_id = "{entra.api_client_id}"\n'
        + (option if table == "auth.profile" else "")
        + "[devices]\n"
    )
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, ValidationError)
    location = (*table.split("."), field)
    assert any(
        entry["loc"] == location and entry["type"] == "extra_forbidden" for entry in error.value.__cause__.errors()
    )


@pytest.mark.parametrize("field", ["tenant_id", "api_client_id"])
@pytest.mark.parametrize("value", [None, "not-a-uuid"], ids=["missing", "invalid"])
def test_entra_profile_requires_uuid_identifiers(tmp_path, entra, field, value):
    identifiers = {"tenant_id": entra.tenant_id, "api_client_id": entra.api_client_id}
    identifiers[field] = value
    path = tmp_path / "invalid-identifier.toml"
    path.write_text(
        '[auth.profile]\ntype = "entra"\n'
        + "".join(f"{name} = {json.dumps(identifier)}\n" for name, identifier in identifiers.items() if identifier)
        + "[devices]\n"
    )
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, ValidationError)
    assert ("auth", "profile", field) in {entry["loc"] for entry in error.value.__cause__.errors()}


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "*",
        "https://*.example.test",
        "https:///missing-host",
        "ftp://reader.example.test",
        "http://reader.example.test",
        "http://192.0.2.10",
        "http://localhost.example.test",
        "https://user:password@reader.example.test",
        "https://reader.example.test/",
        "https://reader.example.test/path",
        "https://reader.example.test?query=value",
        "https://reader.example.test#fragment",
        "https://reader.example.test:bad-port",
        "https://reader.example.test:65536",
        "https://[::1",
    ],
)
def test_auth_rejects_invalid_browser_origins(tmp_path, entra, origin):
    path = tmp_path / "invalid-origin.toml"
    path.write_text(
        f"[auth]\nallowed_origins = [{json.dumps(origin)}]\n"
        + '[auth.profile]\ntype = "entra"\n'
        + f'tenant_id = "{entra.tenant_id}"\napi_client_id = "{entra.api_client_id}"\n[devices]\n'
    )
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, ValidationError)
    assert any(entry["loc"][:2] == ("auth", "allowed_origins") for entry in error.value.__cause__.errors())


def test_auth_preserves_additional_serialized_origins(tmp_path, entra):
    origins = [
        "https://reader.example.test",
        "https://reader.example.test:443",
        "https://192.0.2.10:8443",
        "http://localhost:3000",
        "http://127.0.0.2:8080",
        "http://[::1]:3000",
    ]
    path = tmp_path / "browser-origins.toml"
    path.write_text(
        f"[auth]\nallowed_origins = {json.dumps(origins)}\n"
        + '[auth.profile]\ntype = "entra"\n'
        + f'tenant_id = "{entra.tenant_id}"\napi_client_id = "{entra.api_client_id}"\n[devices]\n'
    )
    assert load_config(path).auth.allowed_origins == origins


def test_auth_omitting_additional_origins_keeps_same_origin_only(tmp_path, entra):
    path = tmp_path / "same-origin.toml"
    path.write_text(
        '[auth.profile]\ntype = "entra"\n'
        + f'tenant_id = "{entra.tenant_id}"\napi_client_id = "{entra.api_client_id}"\n[devices]\n'
    )
    assert load_config(path).auth.allowed_origins == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        '[devices.root]\nclass = "ophyd:Signal"\nclass = "ophyd:Device"',
        '[devices.root]\nclass = "ophyd:Signal"\nkwargs = {name = "other"}',
        'startup_script = "devices.py"\n[devices]',
        '[devices."bad/root"]\nclass = "ophyd:Signal"',
        '[devices.root]\nclass = "ophyd.Signal"',
        '[devices.root]\nclass = "ophyd:Signal()"',
        '[devices.root]\nclass_path = "ophyd:Signal"',
        "[devices.root]\nclass = 42",
        '[devices.root]\nclass = "ophyd:Signal"\nunknown = 1',
        '[devices.root]\nclass = "ophyd:Signal"\nkwargs = {value = 1979-05-27}',
        "connect_timeout = 0\n[devices]",
        "read_timeout = inf\n[devices]",
        "read_timeout = nan\n[devices]",
        "[devices",
    ],
)
def test_invalid_config_reports_file_and_cause(tmp_path, text, entra):
    path = tmp_path / "invalid.toml"
    path.write_text(entra.toml + text)
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert error.value.__cause__ is not None


def test_missing_file_reports_path(tmp_path):
    path: Path = tmp_path / "missing.toml"
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_class_allowlist_excludes_non_device(tmp_path, entra):
    path = tmp_path / "not_device.toml"
    path.write_text(entra.toml + '[devices.root]\nclass = "pathlib:Path"\n')
    spec = load_config(path).devices["root"]
    with pytest.raises(TypeError, match="pathlib:Path"):
        spec.resolve_class()


@pytest.mark.parametrize("invalid_field", ["startup_script", "auth", "auth.profile.type"])
def test_cli_rejects_invalid_config_before_loading_driver_packages(tmp_path, entra, invalid_field):
    import subprocess
    import sys

    path = tmp_path / "invalid-cli.toml"
    if invalid_field == "startup_script":
        text = entra.toml + 'startup_script = "unused.py"\n'
    elif invalid_field == "auth":
        text = ""
    else:
        text = (
            '[auth.profile]\ntype = "generic"\n'
            f'tenant_id = "{entra.tenant_id}"\napi_client_id = "{entra.api_client_id}"\n'
        )
    path.write_text(text + '[devices.root]\nclass = "ophyd:Signal"\n')
    # A fresh interpreter makes import side effects observable even when other
    # tests have already loaded both native packages. Guard the driver boundary.
    program = """
import sys

class GuardDrivers:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"ophyd", "ophyd_async"}:
            raise AssertionError("Driver imported before configuration validation")

sys.meta_path.insert(0, GuardDrivers())
sys.argv = ["ophyd-service", "--config", sys.argv[1]]
from ophyd_as_service.cli import main
main()
"""
    result = subprocess.run([sys.executable, "-c", program, str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2, result.stderr
    assert str(path) in result.stderr
    assert invalid_field in result.stderr


class LifecycleSignal(Signal):
    constructed = []
    destroyed = []
    failure = ValueError("classic lifecycle failure")

    def __init__(self, *, name, failure_phase=None, **kwargs):
        self.constructed.append(name)
        if failure_phase == "construct":
            raise self.failure
        super().__init__(name=name, **kwargs)
        self.failure_phase = failure_phase

    def wait_for_connection(self, timeout=0.0):
        if self.failure_phase == "connect":
            raise self.failure
        return super().wait_for_connection(timeout=timeout)

    def destroy(self):
        self.destroyed.append(self.name)
        super().destroy()
        if self.failure_phase == "destroy":
            raise self.failure


class LifecycleConnector(DeviceConnector):
    async def connect_real(self, device, timeout, force_reconnect):
        device.connection_attempts.append(device.name)
        if device.failure_phase == "connect":
            raise device.failure
        if device.failure_phase == "gate":
            device.connect_entered.set()
            try:
                await device.connect_release.wait()
            finally:
                device.connect_settled.set()
        await super().connect_real(device, timeout, force_reconnect)


class LifecycleAsyncDevice(AsyncDevice):
    constructed = []
    connection_attempts = []
    failure = ConnectionError("async lifecycle failure")

    def __init__(self, *, name, initial_value=1.0, failure_phase=None):
        self.constructed.append(name)
        self.failure_phase = failure_phase
        self.value, _ = soft_signal_r_and_setter(float, initial_value)
        super().__init__(name=name, connector=LifecycleConnector())


@pytest.fixture
def lifecycle_observers(monkeypatch):
    monkeypatch.setattr(LifecycleSignal, "constructed", [])
    monkeypatch.setattr(LifecycleSignal, "destroyed", [])
    monkeypatch.setattr(LifecycleSignal, "failure", ValueError("classic lifecycle failure"))
    monkeypatch.setattr(LifecycleAsyncDevice, "constructed", [])
    monkeypatch.setattr(LifecycleAsyncDevice, "connection_attempts", [])
    monkeypatch.setattr(LifecycleAsyncDevice, "failure", ConnectionError("async lifecycle failure"))


@pytest.mark.asyncio
async def test_registry_constructs_named_roots_once_from_config(tmp_path, lifecycle_observers, entra):
    path = tmp_path / "lifecycle.toml"
    path.write_text(
        entra.toml
        + f'''
    [devices.classic]
    class = "{__name__}:LifecycleSignal"
    [devices.classic.kwargs]
    value = 2.5
    timestamp = 1001.0
    [devices.async]
    class = "{__name__}:LifecycleAsyncDevice"
    [devices.async.kwargs]
    initial_value = 5.678
    '''
    )
    registry = DeviceRegistry(load_config(path))
    assert LifecycleSignal.constructed == []
    assert LifecycleAsyncDevice.constructed == []

    try:
        await registry.start()
        classic = registry.roots["classic"]
        async_root = registry.roots["async"]
        assert classic.name == "classic"
        assert async_root.name == "async"
        assert registry.loop is asyncio.get_running_loop()
        assert await registry.read("classic") == {"classic": {"value": 2.5, "timestamp": 1001.0}}
        readings = await registry.read("async/value")
        assert readings["async-value"]["value"] == 5.678

        with pytest.raises(RuntimeError):
            await registry.start()
        assert registry.roots["classic"] is classic
        assert registry.roots["async"] is async_root
        assert LifecycleSignal.constructed == ["classic"]
        assert LifecycleAsyncDevice.constructed == ["async"]
    finally:
        await registry.close()

    assert LifecycleSignal.destroyed == ["classic"]
    assert registry.roots == {}


@pytest.mark.asyncio
async def test_empty_registry_starts_and_closes(tmp_path, entra):
    path = tmp_path / "empty.toml"
    path.write_text(entra.toml + "[devices]\n")
    registry = DeviceRegistry(load_config(path))
    try:
        await registry.start()
        assert registry.roots == {}
        assert registry.loop is asyncio.get_running_loop()
    finally:
        await registry.close()
    assert registry.roots == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("class_path", "kwargs", "cause_type", "destroyed"),
    [
        ("missing_ophyd_service_lifecycle_driver:Device", {}, ModuleNotFoundError, ["earlier"]),
        ("pathlib:Path", {}, TypeError, ["earlier"]),
        (f"{__name__}:LifecycleSignal", {"failure_phase": "construct"}, ValueError, ["earlier"]),
        (f"{__name__}:LifecycleSignal", {"failure_phase": "connect"}, ValueError, ["broken", "earlier"]),
        (f"{__name__}:LifecycleAsyncDevice", {"failure_phase": "connect"}, ConnectionError, ["earlier"]),
    ],
    ids=["import", "disallowed-class", "constructor", "classic-connect", "async-connect"],
)
async def test_failed_start_preserves_cause_and_releases_owned_roots(
    lifecycle_observers, class_path, kwargs, cause_type, destroyed, entra
):
    config = ServiceConfig.model_validate(
        {
            "auth": entra.auth,
            "devices": {
                "earlier": {"class": f"{__name__}:LifecycleSignal"},
                "broken": {"class": class_path, "kwargs": kwargs},
                "never": {"class": f"{__name__}:LifecycleSignal"},
            },
        }
    )
    registry = DeviceRegistry(config)
    try:
        with pytest.raises(RuntimeError) as error:
            await registry.start()

        assert "broken" in str(error.value)
        assert class_path in str(error.value)
        assert isinstance(error.value.__cause__, cause_type)
        if class_path == f"{__name__}:LifecycleSignal":
            assert error.value.__cause__ is LifecycleSignal.failure
        elif class_path == f"{__name__}:LifecycleAsyncDevice":
            assert error.value.__cause__ is LifecycleAsyncDevice.failure
        assert sorted(LifecycleSignal.destroyed) == destroyed
        assert "never" not in LifecycleSignal.constructed
        assert registry.roots == {}

        constructed = list(LifecycleSignal.constructed)
        async_constructed = list(LifecycleAsyncDevice.constructed)
        connections = list(LifecycleAsyncDevice.connection_attempts)
        with pytest.raises(RuntimeError):
            await registry.start()
        assert LifecycleSignal.constructed == constructed
        assert LifecycleAsyncDevice.constructed == async_constructed
        assert LifecycleAsyncDevice.connection_attempts == connections
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_cancelled_async_connection_aborts_registry(lifecycle_observers, monkeypatch, entra):
    entered = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    monkeypatch.setattr(LifecycleAsyncDevice, "connect_entered", entered, raising=False)
    monkeypatch.setattr(LifecycleAsyncDevice, "connect_release", release, raising=False)
    monkeypatch.setattr(LifecycleAsyncDevice, "connect_settled", settled, raising=False)
    config = ServiceConfig.model_validate(
        {
            "auth": entra.auth,
            "connect_timeout": 30.0,
            "devices": {
                "earlier": {"class": f"{__name__}:LifecycleSignal"},
                "gated": {
                    "class": f"{__name__}:LifecycleAsyncDevice",
                    "kwargs": {"failure_phase": "gate"},
                },
                "never": {"class": f"{__name__}:LifecycleSignal"},
            },
        }
    )
    registry = DeviceRegistry(config)
    startup = asyncio.create_task(registry.start())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, timeout=5.0)

        assert settled.is_set()
        assert LifecycleSignal.destroyed == ["earlier"]
        assert registry.roots == {}
        with pytest.raises(RuntimeError):
            await registry.start()
        assert LifecycleSignal.constructed == ["earlier"]
        assert LifecycleAsyncDevice.constructed == ["gated"]
        assert LifecycleAsyncDevice.connection_attempts == ["gated"]
    finally:
        release.set()
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await registry.close()


@pytest.mark.asyncio
async def test_close_attempts_all_roots_and_preserves_destroy_failure(lifecycle_observers, entra):
    config = ServiceConfig.model_validate(
        {
            "auth": entra.auth,
            "devices": {
                "broken": {
                    "class": f"{__name__}:LifecycleSignal",
                    "kwargs": {"failure_phase": "destroy"},
                },
                "other": {"class": f"{__name__}:LifecycleSignal"},
                "async": {"class": f"{__name__}:LifecycleAsyncDevice"},
            },
        }
    )
    registry = DeviceRegistry(config)
    await registry.start()
    with pytest.raises(ExceptionGroup) as error:
        await registry.close()

    assert error.value.subgroup(lambda exc: exc is LifecycleSignal.failure) is not None
    assert sorted(LifecycleSignal.destroyed) == ["broken", "other"]
    assert registry.roots == {}


def test_app_factory_defers_construction_until_lifespan(tmp_path, lifecycle_observers, entra):
    path = tmp_path / "app-lifecycle.toml"
    path.write_text(
        entra.toml
        + f'''
    [devices.classic]
    class = "{__name__}:LifecycleSignal"
    [devices.classic.kwargs]
    value = 2.5
    timestamp = 1001.0
    [devices.async]
    class = "{__name__}:LifecycleAsyncDevice"
    [devices.async.kwargs]
    initial_value = 5.678
    '''
    )
    app = create_app(load_config(path))
    assert LifecycleSignal.constructed == []
    assert LifecycleAsyncDevice.constructed == []
    assert LifecycleAsyncDevice.connection_attempts == []

    with TestClient(app, headers=entra.headers) as client:
        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        assert response.json() == {"devices": ["classic", "async"]}
        response = client.get("/api/v1/read/classic")
        assert response.status_code == 200
        assert response.json() == {
            "path": "classic",
            "readings": {"classic": {"value": 2.5, "timestamp": 1001.0}},
        }
        response = client.get("/api/v1/read/async/value")
        assert response.status_code == 200
        assert response.json()["readings"]["async-value"]["value"] == 5.678
        assert LifecycleSignal.constructed == ["classic"]
        assert LifecycleAsyncDevice.constructed == ["async"]
        assert LifecycleSignal.destroyed == []

    assert LifecycleSignal.destroyed == ["classic"]


def test_app_lifespan_reads_configured_devices_and_closes_without_mutation(tmp_path, monkeypatch, entra):
    path = tmp_path / "readable-devices.toml"
    path.write_text(
        entra.toml
        + """
    [devices.configured_classic]
    class = "tests.devices:ClassicDevice"
    [devices.configured_async]
    class = "tests.devices:AsyncDevice"
    [devices.configured_async.kwargs]
    initial_value = 5.678
    """
    )
    forbidden_staging = []

    def forbid_staging(signal):
        forbidden_staging.append(signal.name)
        raise AssertionError(f"Lifespan must not stage or unstage {signal.name}")

    monkeypatch.setattr(SignalR, "stage", forbid_staging)
    monkeypatch.setattr(SignalR, "unstage", forbid_staging)
    app = create_app(load_config(path))
    with TestClient(app, headers=entra.headers) as client:
        classic = app.state.registry.roots["configured_classic"]
        async_root = app.state.registry.roots["configured_async"]
        temperature = classic.temperature
        assert not classic.destroyed.is_set()
        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        assert response.json() == {"devices": ["configured_classic", "configured_async"]}
        response = client.get("/api/v1/read/configured_classic")
        assert response.status_code == 200
        assert response.json() == {
            "path": "configured_classic",
            "readings": {"configured_classic_temperature": {"value": 1.0, "timestamp": 1000.0}},
        }
        response = client.get("/api/v1/read/configured_async")
        assert response.status_code == 200
        payload = response.json()
        assert payload["path"] == "configured_async"
        assert set(payload["readings"]) == {"configured_async-temperature"}
        assert payload["readings"]["configured_async-temperature"]["value"] == 5.678

    assert classic.destroyed.is_set()
    assert classic.destroy_calls == 1
    assert temperature.destroyed.is_set()
    assert classic.forbidden_calls == []
    assert temperature.forbidden_calls == []
    assert async_root.forbidden_calls == []
    assert async_root.write_only.forbidden_calls == []
    assert forbidden_staging == []


@pytest.mark.parametrize(
    ("class_path", "failure_phase", "cause_owner", "destroyed"),
    [
        ("pathlib:Path", None, None, ["earlier"]),
        (f"{__name__}:LifecycleSignal", "construct", LifecycleSignal, ["earlier"]),
        (f"{__name__}:LifecycleSignal", "connect", LifecycleSignal, ["broken", "earlier"]),
        (f"{__name__}:LifecycleAsyncDevice", "connect", LifecycleAsyncDevice, ["earlier"]),
    ],
    ids=["disallowed-class", "constructor", "classic-connect", "async-connect"],
)
def test_app_startup_failure_never_serves_and_releases_owned_roots(
    tmp_path, lifecycle_observers, class_path, failure_phase, cause_owner, destroyed, entra
):
    path = tmp_path / "failed-lifespan.toml"
    kwargs = f'kwargs = {{failure_phase = "{failure_phase}"}}\n' if failure_phase else ""
    path.write_text(
        entra.toml
        + f'''
    [devices.earlier]
    class = "{__name__}:LifecycleSignal"
    [devices.broken]
    class = "{class_path}"
    {kwargs}
    [devices.never]
    class = "{__name__}:LifecycleSignal"
    '''
    )
    with pytest.raises(RuntimeError) as error:
        with TestClient(create_app(load_config(path)), headers=entra.headers):
            pytest.fail("An application with a failed configured root became ready")

    assert "broken" in str(error.value)
    assert class_path in str(error.value)
    if cause_owner is None:
        assert isinstance(error.value.__cause__, TypeError)
        assert class_path in str(error.value.__cause__)
    else:
        assert error.value.__cause__ is cause_owner.failure
    assert sorted(LifecycleSignal.destroyed) == destroyed
    assert "never" not in LifecycleSignal.constructed


@pytest.mark.parametrize("endpoint", ["discovery_route", "jwks_route", "browser-origin"])
def test_signing_key_failure_prevents_native_root_construction(lifecycle_observers, entra, endpoint):
    if endpoint == "browser-origin":
        origin = "https://reader.example.test"
        entra.auth["allowed_origins"] = [origin]
        entra.discovery_route.respond(200, json={"issuer": entra.issuer, "jwks_uri": f"{origin}/jwks"})
    else:
        getattr(entra, endpoint).respond(503)
    config = ServiceConfig.model_validate(
        {
            "auth": entra.auth,
            "devices": {
                "classic": {"class": f"{__name__}:LifecycleSignal"},
                "async": {"class": f"{__name__}:LifecycleAsyncDevice"},
            },
        }
    )
    with pytest.raises(ServiceError) as error:
        with TestClient(create_app(config)):
            pytest.fail("Application became ready without signing keys")
    assert error.value.code == "auth_unavailable"
    assert error.value.status == 503
    assert LifecycleSignal.constructed == []
    assert LifecycleAsyncDevice.constructed == []
    assert LifecycleAsyncDevice.connection_attempts == []
