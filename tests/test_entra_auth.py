import asyncio
import json
import logging
import time
from contextlib import contextmanager

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from ophyd_as_service import api, auth, subscriptions
from ophyd_as_service.api import create_app
from ophyd_as_service.auth import JWT_LEEWAY_SECONDS, Principal
from ophyd_as_service.auth_profiles import build_authenticator
from ophyd_as_service.config import AuthConfig, ServiceConfig
from ophyd_as_service.errors import ServiceError
from ophyd_as_service.subscriptions import WS_MAX_MESSAGE_BYTES
from tests.test_subscriptions import RaceSendGate, RaceSendMiddleware, _error, _reading, _receive, _subscribe


@pytest_asyncio.fixture
async def authenticator(entra):
    authenticator = build_authenticator(AuthConfig.model_validate(entra.auth))
    try:
        await authenticator.start()
        yield authenticator
    finally:
        await authenticator.close()


def signed_claims(entra, claims, *, kid=None, private_key=None):
    return jwt.encode(
        claims,
        entra.signing_key.private_key if private_key is None else private_key,
        algorithm="RS256",
        headers={"kid": entra.signing_key.kid if kid is None else kid},
    )


async def assert_denied(authenticator, token, *, code, status):
    with pytest.raises(ServiceError) as caught:
        await authenticator.require_reader(token)
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.path is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "app"])
async def test_readers_receive_verified_opaque_identity(authenticator, entra, kind):
    claims = entra.claims(kind=kind)
    claims["sub"] = "collector:west/17"
    claims["roles"] = ["Other.Role", "Ophyd.Reader"]
    if kind == "user":
        claims["scp"] = "Other.Scope Ophyd.Read"
    principal = await authenticator.require_reader(signed_claims(entra, claims))
    assert principal == Principal(
        issuer=entra.issuer,
        subject="collector:west/17",
        expires_at=claims["exp"] + JWT_LEEWAY_SECONDS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "app"])
@pytest.mark.parametrize(
    "roles",
    [None, [], ["Other.Role"], ["prefix.Ophyd.Reader"]],
    ids=["missing", "empty", "different", "substring"],
)
async def test_tenant_membership_without_reader_role_is_forbidden(authenticator, entra, kind, roles):
    claims = entra.claims(kind=kind)
    if roles is None:
        del claims["roles"]
    else:
        claims["roles"] = roles
    await assert_denied(authenticator, signed_claims(entra, claims), code="forbidden", status=403)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [None, "", "Other.Scope", "Ophyd.Reader", "prefix.Ophyd.Read", "Other.Scope\tOphyd.Read"],
    ids=["missing", "empty", "different", "role-is-not-scope", "substring", "not-space-delimited"],
)
async def test_delegated_reader_also_needs_exact_scope(authenticator, entra, scope):
    claims = entra.claims()
    if scope is None:
        del claims["scp"]
    else:
        claims["scp"] = scope
    await assert_denied(authenticator, signed_claims(entra, claims), code="forbidden", status=403)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["iat", "nbf", "tid", "ver", "idtyp", "oid", "azp"])
async def test_provider_required_claims_cannot_be_omitted(authenticator, entra, claim):
    claims = entra.claims()
    del claims[claim]
    await assert_denied(authenticator, signed_claims(entra, claims), code="unauthenticated", status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"ver": "1.0"}, id="v1-token"),
        pytest.param({"ver": 2.0}, id="numeric-version"),
        pytest.param({"tid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}, id="foreign-tenant"),
        pytest.param({"tid": "not-a-guid"}, id="malformed-tenant"),
        pytest.param({"tid": None}, id="null-tenant"),
        pytest.param({"oid": "not-a-guid"}, id="malformed-object"),
        pytest.param({"oid": 123}, id="numeric-object"),
        pytest.param({"azp": "not-a-guid"}, id="malformed-client"),
        pytest.param({"azp": []}, id="list-client"),
        pytest.param({"idtyp": "service"}, id="unknown-kind"),
        pytest.param({"idtyp": ["user"]}, id="list-kind"),
        pytest.param({"idtyp": None}, id="null-kind"),
        pytest.param({"roles": "Ophyd.Reader"}, id="scalar-roles"),
        pytest.param({"roles": ["Ophyd.Reader", 123]}, id="mixed-roles"),
        pytest.param({"roles": None}, id="null-roles"),
        pytest.param({"scp": ["Ophyd.Read"]}, id="list-scope"),
        pytest.param({"scp": None}, id="null-scope"),
    ],
)
async def test_malformed_provider_claims_are_not_permission_denials(authenticator, entra, overrides):
    await assert_denied(authenticator, entra.token(claims=overrides), code="unauthenticated", status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["Ophyd.Read", "", None], ids=["delegated-scope", "empty-scope", "null-scope"])
async def test_app_tokens_must_not_have_scope_claim(authenticator, entra, scope):
    await assert_denied(
        authenticator,
        entra.token(kind="app", claims={"scp": scope}),
        code="unauthenticated",
        status=401,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("substitution", ["id-token", "v1-client", "email-identity"])
async def test_id_token_and_legacy_identity_substitutions_are_rejected(authenticator, entra, substitution):
    claims = entra.claims()
    if substitution == "id-token":
        del claims["idtyp"]
        del claims["scp"]
        claims["nonce"] = "id-token-nonce"
    elif substitution == "v1-client":
        claims["appid"] = claims.pop("azp")
    else:
        del claims["oid"]
        claims["email"] = "reader@example.test"
        claims["upn"] = "reader@example.test"
    await assert_denied(authenticator, signed_claims(entra, claims), code="unauthenticated", status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"iss": "https://issuer.example.test"}, id="foreign-issuer"),
        pytest.param({"aud": "https://graph.microsoft.com"}, id="foreign-resource"),
    ],
)
async def test_entra_reader_claims_do_not_bypass_common_trust(authenticator, entra, overrides):
    await assert_denied(authenticator, entra.token(claims=overrides), code="unauthenticated", status=401)


@pytest.mark.asyncio
async def test_entra_reader_claims_do_not_bypass_signature(authenticator, entra):
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = signed_claims(entra, entra.claims(), private_key=untrusted_key)
    await assert_denied(authenticator, token, code="unauthenticated", status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["absent", "exact", "template"])
async def test_entra_signing_key_issuer_rules_accept_trusted_keys(entra, extension):
    jwk = dict(entra.signing_key.public_jwk)
    if extension == "exact":
        jwk["issuer"] = entra.issuer
    elif extension == "template":
        jwk["issuer"] = "https://login.microsoftonline.com/{tenantid}/v2.0"
    entra.jwks_route.respond(200, json={"keys": [jwk]})
    authenticator = build_authenticator(AuthConfig.model_validate(entra.auth))
    try:
        await authenticator.start()
        principal = await authenticator.require_reader(entra.token())
        assert principal.issuer == entra.issuer
        assert principal.subject == entra.object_id
    finally:
        await authenticator.close()


@pytest.mark.asyncio
async def test_foreign_key_issuer_is_filtered_without_discarding_trusted_keys(entra):
    foreign_key = {
        **entra.signing_key.public_jwk,
        "kid": "foreign-tenant-key",
        "issuer": "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/v2.0",
    }
    trusted_key = {**entra.signing_key.public_jwk, "issuer": entra.issuer}
    entra.jwks_route.respond(200, json={"keys": [foreign_key, trusted_key]})
    authenticator = build_authenticator(AuthConfig.model_validate(entra.auth))
    try:
        await authenticator.start()
        await assert_denied(
            authenticator,
            signed_claims(entra, entra.claims(), kid=foreign_key["kid"]),
            code="unauthenticated",
            status=401,
        )
        principal = await authenticator.require_reader(entra.token())
        assert principal.subject == entra.object_id
    finally:
        await authenticator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issuer",
    [
        pytest.param("https://issuer.example.test/v2.0", id="foreign-host"),
        pytest.param("https://login.microsoftonline.com/{tenantid}/v1.0", id="wrong-version"),
        pytest.param("https://login.microsoftonline.com/{tenantId}/v2.0", id="unsupported-template"),
    ],
)
async def test_key_set_without_in_scope_issuer_cannot_start(entra, issuer):
    jwk = {**entra.signing_key.public_jwk, "issuer": issuer}
    entra.jwks_route.respond(200, json={"keys": [jwk]})
    authenticator = build_authenticator(AuthConfig.model_validate(entra.auth))
    try:
        with pytest.raises(ServiceError) as caught:
            await authenticator.start()
        assert caught.value.code == "auth_unavailable"
        assert caught.value.status == 503
        assert caught.value.path is None
    finally:
        await authenticator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer", [None, 123, ["https://login.microsoftonline.com/{tenantid}/v2.0"]])
async def test_malformed_key_issuer_fails_refresh_even_with_another_usable_key(entra, issuer):
    malformed_key = {**entra.signing_key.public_jwk, "kid": "malformed-key", "issuer": issuer}
    trusted_key = {**entra.signing_key.public_jwk, "issuer": entra.issuer}
    entra.jwks_route.respond(200, json={"keys": [trusted_key, malformed_key]})
    authenticator = build_authenticator(AuthConfig.model_validate(entra.auth))
    try:
        with pytest.raises(ServiceError) as caught:
            await authenticator.start()
        assert caught.value.code == "auth_unavailable"
        assert caught.value.status == 503
        assert caught.value.path is None
    finally:
        await authenticator.close()


class _WithoutDenialExtension:
    """Exercise servers lacking only the optional WebSocket denial extension."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            extensions = dict(scope.get("extensions", {}))
            extensions.pop("websocket.http.response", None)
            scope = {**scope, "extensions": extensions}
        await self.app(scope, receive, send)


@contextmanager
def _transport_serving(entra, *, allowed_origins=(), denial_extension=True, gate=None):
    config = ServiceConfig.model_validate(
        {
            "auth": {**entra.auth, "allowed_origins": list(allowed_origins)},
            "devices": {"classic": {"class": "tests.devices:ClassicDevice"}},
        }
    )
    app = create_app(config)
    if not denial_extension:
        app.add_middleware(_WithoutDenialExtension)
    if gate is not None:
        app.add_middleware(RaceSendMiddleware, gate=gate)
    # Never inherit bearer credentials: each positive request supplies its own.
    with TestClient(app) as client:
        yield client, app.state.registry.roots["classic"]


@pytest.fixture
def transport_service(entra):
    with _transport_serving(entra) as service:
        yield service


def _assert_no_device_access(classic):
    assert classic.temperature.read_calls == 0
    assert classic.lazy_constructions == []
    assert not classic.unused_constructed.is_set()
    assert classic.temperature.subscribe_calls == 0
    assert classic.temperature.active_tokens == set()


def _assert_http_auth_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "path"}
    assert error["code"] == code
    assert error["path"] is None
    assert isinstance(error["message"], str)
    assert "Traceback" not in error["message"]
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


def _assert_closed(ws, code):
    with pytest.raises(WebSocketDisconnect) as closed:
        _receive(ws)
    assert closed.value.code == code
    return closed.value


def _make_keys_unavailable(entra, monkeypatch):
    # Expire the real cache, then fail only its real, exact JWKS HTTP boundary.
    now = auth.monotonic()
    monkeypatch.setattr(auth, "monotonic", lambda: now + auth.KEY_MAX_AGE_SECONDS)
    entra.jwks_route.respond(503)


_HTTP_ROUTES = (
    "/api/v1/devices",
    "/api/v1/resources/classic/unused",
    "/api/v1/read/classic/temperature",
    "/api/v1/describe/classic/unused",
)


@pytest.mark.parametrize("route", _HTTP_ROUTES)
@pytest.mark.parametrize("credential", ["missing", "invalid", "forbidden"])
def test_http_reader_gate_precedes_all_device_access(transport_service, entra, route, credential):
    client, classic = transport_service
    headers = {}
    if credential == "invalid":
        headers["Authorization"] = f"Bearer {entra.token(claims={'aud': 'another-api'})}"
    elif credential == "forbidden":
        headers["Authorization"] = f"Bearer {entra.token(claims={'roles': []})}"
    response = client.get(route, headers=headers)
    if credential == "forbidden":
        _assert_http_auth_error(response, 403, "forbidden")
    else:
        _assert_http_auth_error(response, 401, "unauthenticated")
    _assert_no_device_access(classic)


@pytest.mark.parametrize("kind", ["user", "app"])
def test_http_delegated_and_app_readers_reach_all_four_routes(transport_service, entra, kind):
    client, classic = transport_service
    headers = {"Authorization": f"Bearer {entra.token(kind=kind)}"}
    responses = [client.get(route, headers=headers) for route in _HTTP_ROUTES]
    for response in responses:
        assert response.status_code == 200, response.text
    inventory, resource, reading, description = [response.json() for response in responses]
    assert inventory == {"devices": ["classic"]}
    assert resource == {
        "path": "classic/unused",
        "backend": "ophyd",
        "readable": True,
        "monitorable": True,
        "children": [],
    }
    assert reading == {
        "path": "classic/temperature",
        "readings": {"classic_temperature": {"value": 1.0, "timestamp": 1000.0}},
    }
    assert description == {"path": "classic/unused", "data_keys": classic.unused.describe()}
    assert classic.temperature.read_calls == 1
    assert classic.temperature.active_tokens == set()


@pytest.mark.parametrize("route", _HTTP_ROUTES)
def test_http_unavailable_signing_keys_do_not_use_stale_trust(transport_service, entra, monkeypatch, route):
    client, classic = transport_service
    _make_keys_unavailable(entra, monkeypatch)
    _assert_http_auth_error(client.get(route, headers=entra.headers), 503, "auth_unavailable")
    _assert_no_device_access(classic)


def _malformed_authorization(entra, form):
    valid = entra.headers["Authorization"]
    if form == "duplicates":
        return httpx.Headers([("Authorization", valid), ("Authorization", valid)])
    value = {"empty": "", "empty-bearer": "Bearer", "non-bearer": valid.replace("Bearer", "Basic", 1)}[form]
    return httpx.Headers({"Authorization": value})


@pytest.mark.parametrize("form", ["empty", "empty-bearer", "non-bearer", "duplicates"])
def test_http_rejects_ambiguous_or_non_bearer_authorization(transport_service, entra, form):
    client, classic = transport_service
    response = client.get("/api/v1/read/classic/temperature", headers=_malformed_authorization(entra, form))
    _assert_http_auth_error(response, 401, "unauthenticated")
    _assert_no_device_access(classic)


@pytest.mark.parametrize("source", ["query", "cookie", "forwarded-user"])
def test_http_ignores_alternative_identity_sources(transport_service, entra, source):
    client, classic = transport_service
    token = entra.token()
    params = {"access_token": token} if source == "query" else {}
    headers = {"X-Forwarded-User": entra.object_id} if source == "forwarded-user" else {}
    if source == "cookie":
        client.cookies.set("access_token", token)
        client.cookies.set("Authorization", f"Bearer {token}")
    _assert_http_auth_error(client.get("/api/v1/devices", params=params, headers=headers), 401, "unauthenticated")
    _assert_no_device_access(classic)


@pytest.mark.parametrize("origin", [None, "http://testserver", "https://reader.example.test"])
def test_websocket_header_reader_keeps_native_ack_then_reading(entra, origin):
    with _transport_serving(entra, allowed_origins=["https://reader.example.test"]) as (client, classic):
        headers = dict(entra.headers)
        if origin is not None:
            headers["Origin"] = origin
        with client.websocket_connect("/api/v1/ws", headers=headers) as ws:
            _reading(
                _subscribe(ws, "classic/temperature"), "classic/temperature", "classic_temperature", 1.0, 1000.0
            )
        assert classic.temperature.unsubscribed.wait(timeout=5)
        assert classic.temperature.active_tokens == set()


@pytest.mark.parametrize("denial_extension", [True, False], ids=["http-denial", "close-fallback"])
@pytest.mark.parametrize(
    "failure,status,code,close_code",
    [
        ("invalid", 401, "unauthenticated", 1008),
        ("forbidden", 403, "forbidden", 1008),
        ("unavailable", 503, "auth_unavailable", 1013),
    ],
)
def test_websocket_header_denials_are_preaccept_and_fail_closed(
    entra, monkeypatch, denial_extension, failure, status, code, close_code
):
    with _transport_serving(entra, denial_extension=denial_extension) as (client, classic):
        token = entra.token()
        if failure == "invalid":
            token = entra.token(claims={"aud": "another-api"})
        elif failure == "forbidden":
            token = entra.token(claims={"roles": []})
        else:
            _make_keys_unavailable(entra, monkeypatch)
        expected = WebSocketDenialResponse if denial_extension else WebSocketDisconnect
        with pytest.raises(expected) as denied:
            with client.websocket_connect("/api/v1/ws", headers={"Authorization": f"Bearer {token}"}):
                pytest.fail("Rejected upgrade entered a WebSocket session")
        if denial_extension:
            _assert_http_auth_error(denied.value, status, code)
        else:
            assert denied.value.code == close_code
        _assert_no_device_access(classic)


@pytest.mark.parametrize("form", ["empty", "empty-bearer", "non-bearer", "duplicates"])
def test_websocket_supplied_header_never_falls_back_to_browser_frame(transport_service, entra, form):
    client, classic = transport_service
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/api/v1/ws", headers=_malformed_authorization(entra, form)) as ws:
            ws.send_json({"op": "authenticate", "access_token": entra.token()})
            pytest.fail("An invalid authoritative header reached browser authentication")
    _assert_http_auth_error(denied.value, 401, "unauthenticated")
    _assert_no_device_access(classic)


@pytest.mark.parametrize("origin", ["null", "https://elsewhere.example.test", "https://reader.example.test:444"])
def test_websocket_reader_token_does_not_override_origin_policy(entra, origin):
    with _transport_serving(entra, allowed_origins=["https://reader.example.test"]) as (client, classic):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws", headers={**entra.headers, "Origin": origin}):
                pytest.fail("Disallowed origin was accepted")
        _assert_no_device_access(classic)


@pytest.mark.parametrize("origin", ["http://testserver", "https://reader.example.test"])
def test_browser_authenticates_before_any_command_and_receives_effective_deadline(entra, origin):
    claims = entra.claims()
    token = signed_claims(entra, claims)
    with _transport_serving(entra, allowed_origins=["https://reader.example.test"]) as (client, classic):
        with client.websocket_connect("/api/v1/ws", headers={"Origin": origin}) as ws:
            _assert_no_device_access(classic)
            ws.send_json({"op": "authenticate", "access_token": token})
            assert _receive(ws) == {"type": "authenticated", "expires_at": claims["exp"] + JWT_LEEWAY_SECONDS}
            _assert_no_device_access(classic)
            _reading(
                _subscribe(ws, "classic/temperature"), "classic/temperature", "classic_temperature", 1.0, 1000.0
            )
        assert classic.temperature.unsubscribed.wait(timeout=5)
        assert classic.temperature.active_tokens == set()


@pytest.mark.parametrize("source", ["none", "query", "subprotocol", "cookie", "forwarded-user"])
def test_websocket_cannot_subscribe_using_alternative_credentials(transport_service, entra, source):
    client, classic = transport_service
    token = entra.token()
    url = f"/api/v1/ws?access_token={token}" if source == "query" else "/api/v1/ws"
    subprotocols = ["bearer", token] if source == "subprotocol" else None
    headers = {"X-Forwarded-User": entra.object_id} if source == "forwarded-user" else {}
    if source == "cookie":
        client.cookies.set("access_token", token)
        client.cookies.set("Authorization", f"Bearer {token}")
    with client.websocket_connect(url, subprotocols=subprotocols, headers=headers) as ws:
        ws.send_json({"id": "premature", "op": "subscribe", "path": "classic/unused"})
        _error(_receive(ws), None, None, "unauthenticated")
        _assert_closed(ws, 1008)
    _assert_no_device_access(classic)


def test_websocket_authentication_timeout_closes_without_device_access(transport_service, monkeypatch):
    client, classic = transport_service
    monkeypatch.setattr(api, "WS_AUTH_TIMEOUT_SECONDS", 0.05)
    with client.websocket_connect("/api/v1/ws") as ws:
        _error(_receive(ws), None, None, "unauthenticated")
        _assert_closed(ws, 1008)
    _assert_no_device_access(classic)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("{", id="malformed-json"),
        pytest.param("[]", id="non-object"),
        pytest.param('{"op":"authenticate"}', id="missing-token"),
        pytest.param('{"op":"authenticate","access_token":123}', id="non-string-token"),
        pytest.param('{"op":"authenticate","access_token":""}', id="empty-token"),
        pytest.param('{"op":"AUTHENTICATE","access_token":"token"}', id="wrong-operation"),
    ],
)
def test_websocket_malformed_first_frame_is_not_a_command(transport_service, payload):
    client, classic = transport_service
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text(payload)
        _error(_receive(ws), None, None, "unauthenticated")
        _assert_closed(ws, 1008)
    _assert_no_device_access(classic)


def test_websocket_authentication_rejects_extra_fields_even_with_valid_token(transport_service, entra):
    client, classic = transport_service
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"op": "authenticate", "access_token": entra.token(), "path": "classic/unused"})
        _error(_receive(ws), None, None, "unauthenticated")
        _assert_closed(ws, 1008)
    _assert_no_device_access(classic)


@pytest.mark.parametrize(
    "failure,code,close_code",
    [
        ("invalid", "unauthenticated", 1008),
        ("forbidden", "forbidden", 1008),
        ("unavailable", "auth_unavailable", 1013),
    ],
)
def test_browser_tokens_use_the_same_reader_policy(
    transport_service, entra, monkeypatch, failure, code, close_code
):
    client, classic = transport_service
    token = entra.token()
    if failure == "invalid":
        token = entra.token(claims={"aud": "another-api"})
    elif failure == "forbidden":
        token = entra.token(claims={"roles": []})
    else:
        _make_keys_unavailable(entra, monkeypatch)
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"op": "authenticate", "access_token": token})
        _error(_receive(ws), None, None, code)
        _assert_closed(ws, close_code)
    _assert_no_device_access(classic)


def test_websocket_binary_authentication_is_rejected(transport_service, entra):
    client, classic = transport_service
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_bytes(json.dumps({"op": "authenticate", "access_token": entra.token()}).encode())
        _assert_closed(ws, 1003)
    _assert_no_device_access(classic)


def test_websocket_authentication_size_limit_counts_utf8_bytes(transport_service, entra):
    client, classic = transport_service
    oversized = json.dumps(
        {"op": "authenticate", "access_token": "é" * (WS_MAX_MESSAGE_BYTES // 2)}, ensure_ascii=False
    )
    assert len(oversized) < WS_MAX_MESSAGE_BYTES < len(oversized.encode("utf-8"))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text(oversized)
        _assert_closed(ws, 1009)
    _assert_no_device_access(classic)


def test_websocket_authentication_accepts_exact_message_size_boundary(transport_service, entra):
    client, classic = transport_service
    claims = entra.claims()
    payload = json.dumps({"op": "authenticate", "access_token": signed_claims(entra, claims)})
    payload += " " * (WS_MAX_MESSAGE_BYTES - len(payload.encode("utf-8")))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text(payload)
        assert _receive(ws) == {"type": "authenticated", "expires_at": claims["exp"] + JWT_LEEWAY_SECONDS}
    _assert_no_device_access(classic)


def test_browser_disconnect_during_authentication_leaves_no_native_membership(transport_service):
    client, classic = transport_service
    with client.websocket_connect("/api/v1/ws"):
        _assert_no_device_access(classic)
    _assert_no_device_access(classic)


def test_authenticated_socket_rejects_in_place_identity_replacement(transport_service, entra):
    client, classic = transport_service
    claims = entra.claims()
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"op": "authenticate", "access_token": signed_claims(entra, claims)})
        assert _receive(ws) == {"type": "authenticated", "expires_at": claims["exp"] + JWT_LEEWAY_SECONDS}
        _subscribe(ws, "classic/temperature")
        ws.send_json({"op": "authenticate", "access_token": entra.token(claims={"sub": "another-reader"})})
        _error(_receive(ws), None, None, "invalid_request")
        classic.temperature.fixture_put(2.0, timestamp=1002.0)
        _reading(_receive(ws), "classic/temperature", "classic_temperature", 2.0, 1002.0)
        assert classic.temperature.subscribe_calls == 1
    assert classic.temperature.unsubscribed.wait(timeout=5)
    assert classic.temperature.active_tokens == set()


def test_extra_browser_origin_does_not_grant_reader_access(entra):
    origin = "https://reader.example.test"
    with _transport_serving(entra, allowed_origins=[origin]) as (client, classic):
        response = client.get("/api/v1/devices", headers={"Origin": origin})
        _assert_http_auth_error(response, 401, "unauthenticated")
        assert response.headers["access-control-allow-origin"] == origin
        assert "access-control-allow-credentials" not in response.headers
        with client.websocket_connect("/api/v1/ws", headers={"Origin": origin}) as ws:
            ws.send_json({"id": "premature", "op": "subscribe", "path": "classic/unused"})
            _error(_receive(ws), None, None, "unauthenticated")
            _assert_closed(ws, 1008)
        _assert_no_device_access(classic)


def test_cors_preflight_is_public_but_exact_and_read_only(entra):
    origin = "https://reader.example.test"
    with _transport_serving(entra, allowed_origins=[origin]) as (client, classic):
        headers = {
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        }
        response = client.options("/api/v1/read/classic/unused", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-methods"] == "GET"
        allowed_headers = {
            header.strip().lower() for header in response.headers["access-control-allow-headers"].split(",")
        }
        assert "authorization" in allowed_headers
        assert "access-control-allow-credentials" not in response.headers
        for override in (
            {"Origin": "https://elsewhere.example.test"},
            {"Origin": f"{origin}:444"},
            {"Access-Control-Request-Method": "POST"},
            {"Access-Control-Request-Headers": "Authorization, X-Other"},
        ):
            rejected = client.options("/api/v1/read/classic/unused", headers={**headers, **override})
            assert rejected.status_code == 400
            if "Origin" in override:
                assert "access-control-allow-origin" not in rejected.headers
            assert "access-control-allow-credentials" not in rejected.headers
        _assert_no_device_access(classic)


def test_empty_origin_allowlist_does_not_enable_cors(transport_service, entra):
    client, classic = transport_service
    origin = "https://reader.example.test"
    response = client.get("/api/v1/devices", headers={**entra.headers, "Origin": origin})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/v1/devices",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers
    _assert_no_device_access(classic)


def test_public_schema_and_normal_routing_do_not_reveal_devices(transport_service):
    client, classic = transport_service
    for route in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(route)
        assert response.status_code == 200
        assert "classic_temperature" not in response.text
        assert "classic/unused" not in response.text
    schema = client.get("/openapi.json").json()
    bearer = schema["components"]["securitySchemes"]["JWTAccessToken"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    for route in ("/api/v1/devices", "/api/v1/resources/{path}", "/api/v1/read/{path}", "/api/v1/describe/{path}"):
        assert schema["paths"][route]["get"]["security"] == [{"JWTAccessToken": []}]
    assert client.get("/not-a-route").status_code == 404
    assert client.post("/api/v1/read/classic/temperature", json={"value": 5}).status_code == 405
    _assert_http_auth_error(client.get("/api/v1/devices"), 401, "unauthenticated")
    _assert_no_device_access(classic)


def test_auth_rejections_never_echo_tokens_claims_or_model_inputs(transport_service, entra, caplog):
    client, classic = transport_service
    caplog.set_level(logging.DEBUG, logger="ophyd_as_service")
    marker = "private-claim-marker-do-not-disclose"
    claims = {**entra.claims(), "aud": "another-api", "sub": marker, "private_context": marker}
    token = signed_claims(entra, claims)
    response = client.get("/api/v1/devices", headers={"Authorization": f"Bearer {token}"})
    _assert_http_auth_error(response, 401, "unauthenticated")
    responses = [response.text]
    for frame in (
        {"op": "authenticate", "access_token": token},
        {"op": "authenticate", "access_token": token, "private_input": marker},
    ):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json(frame)
            error = _error(_receive(ws), None, None, "unauthenticated")
            responses.append(json.dumps(error))
            _assert_closed(ws, 1008)
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/api/v1/ws", headers={"Authorization": f"Bearer {token}"}):
            pytest.fail("Invalid reader was accepted")
    _assert_http_auth_error(denied.value, 401, "unauthenticated")
    responses.append(denied.value.text)
    for output in (*responses, caplog.text):
        assert token not in output
        assert marker not in output
        assert "private_context" not in output
        assert "private_input" not in output
        assert entra.tenant_id not in output
        assert entra.api_client_id not in output
    _assert_no_device_access(classic)


@pytest.fixture
def expiring_reader(entra, monkeypatch):
    # Only the focused lifetime checks remove skew; the generic JWT suite and
    # launched smoke exercise the real 30-second validation tolerance.
    monkeypatch.setattr(auth, "JWT_LEEWAY_SECONDS", 0)

    def mint():
        deadline = int(time.time()) + 2
        return entra.token(claims={"exp": deadline}), deadline

    return mint


@pytest.mark.parametrize("transport", ["header", "first-frame"])
def test_quiet_expiry_closes_and_releases_native_callback(transport_service, expiring_reader, transport):
    client, classic = transport_service
    token, deadline = expiring_reader()
    headers = {"Authorization": f"Bearer {token}"} if transport == "header" else {"Origin": "http://testserver"}
    with client.websocket_connect("/api/v1/ws", headers=headers) as ws:
        if transport == "first-frame":
            ws.send_json({"op": "authenticate", "access_token": token})
            assert _receive(ws) == {"type": "authenticated", "expires_at": deadline}
        _subscribe(ws, "classic/temperature")
        assert _assert_closed(ws, 1008).reason == "token_expired"
        assert classic.temperature.unsubscribed.wait(timeout=5)
        assert classic.temperature.active_tokens == set()


def test_expiry_does_not_revoke_another_reader_of_the_shared_signal(transport_service, entra, expiring_reader):
    client, classic = transport_service
    path = "classic/temperature"
    signal = classic.temperature
    with client.websocket_connect("/api/v1/ws", headers=entra.headers) as remaining:
        _subscribe(remaining, path, id="long")
        token, _ = expiring_reader()
        with client.websocket_connect("/api/v1/ws", headers={"Authorization": f"Bearer {token}"}) as expiring:
            _subscribe(expiring, path, id="short")
            assert _assert_closed(expiring, 1008).reason == "token_expired"
            signal.fixture_put(7.5, timestamp=2000.0)
            _reading(_receive(remaining), path, "classic_temperature", 7.5, timestamp=2000.0)
            remaining.send_json({"id": "stop", "op": "unsubscribe", "path": path})
            assert _receive(remaining) == {"type": "unsubscribed", "id": "stop", "path": path}
        assert signal.unsubscribed.wait(timeout=5)
        assert signal.active_tokens == set()
        assert signal.subscription_tokens == signal.unsubscription_tokens


def test_expiry_removes_a_late_successful_classic_registration(entra, expiring_reader):
    config = ServiceConfig.model_validate(
        {"auth": entra.auth, "devices": {"gated": {"class": "tests.devices:GatedSignal"}}}
    )
    app = create_app(config)
    with TestClient(app) as client:
        signal = app.state.registry.roots["gated"]
        signal.subscribe_release.clear()
        token, _ = expiring_reader()
        try:
            with client.websocket_connect("/api/v1/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
                ws.send_json({"id": "pending", "op": "subscribe", "path": "gated"})
                assert signal.subscribe_started.wait(timeout=5)
                assert _assert_closed(ws, 1008).reason == "token_expired"
        finally:
            signal.subscribe_release.set()
        assert signal.subscribe_finished.wait(timeout=5)
        assert signal.unsubscribed.wait(timeout=5)
        assert signal.active_tokens == set()
        assert signal.subscription_tokens == signal.unsubscription_tokens


@pytest.mark.parametrize("action", ["command", "send"])
def test_expiry_is_checked_before_new_commands_and_sends(transport_service, entra, monkeypatch, action):
    client, classic = transport_service
    claims = entra.claims()
    token = signed_claims(entra, claims)
    with client.websocket_connect("/api/v1/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
        _subscribe(ws, "classic/temperature")
        # Simulate a forward wall-clock adjustment while the quiet waiter is
        # sleeping. Admission/sending must independently enforce the deadline.
        monkeypatch.setattr(subscriptions, "time", lambda: claims["exp"] + JWT_LEEWAY_SECONDS)
        if action == "command":
            ws.send_json({"id": "late", "op": "subscribe", "path": "classic/unused"})
        else:
            classic.temperature.fixture_put(19.0, timestamp=2001.0)
        assert _assert_closed(ws, 1008).reason == "token_expired"
        assert classic.temperature.unsubscribed.wait(timeout=5)
        assert classic.temperature.active_tokens == set()
        assert classic.lazy_constructions == []


def test_deadline_elapsed_before_broker_admission_creates_no_native_interest(
    transport_service, entra, monkeypatch
):
    client, classic = transport_service
    claims = entra.claims()
    monkeypatch.setattr(subscriptions, "time", lambda: claims["exp"] + JWT_LEEWAY_SECONDS)
    with client.websocket_connect(
        "/api/v1/ws", headers={"Authorization": f"Bearer {signed_claims(entra, claims)}"}
    ) as ws:
        assert _assert_closed(ws, 1008).reason == "token_expired"
    _assert_no_device_access(classic)


class _AuthenticationSendGate(RaceSendGate):
    async def send(self, message, send):
        if message["type"] == "websocket.send" and json.loads(message["text"]).get("type") == "authenticated":
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        await send(message)


@pytest.mark.parametrize("limit", ["send-timeout", "token-expiry"])
def test_stalled_authentication_acknowledgement_never_admits_commands(entra, expiring_reader, monkeypatch, limit):
    gate = _AuthenticationSendGate()
    if limit == "send-timeout":
        monkeypatch.setattr(api, "WS_SEND_TIMEOUT", 0.05)
    with _transport_serving(entra, gate=gate) as (client, classic):
        token = entra.token() if limit == "send-timeout" else expiring_reader()[0]
        try:
            with client.websocket_connect("/api/v1/ws?slow=1") as ws:
                ws.send_json({"op": "authenticate", "access_token": token})
                ws.send_json({"id": "premature", "op": "subscribe", "path": "classic/unused"})
                assert gate.entered.wait(timeout=5)
                code, reason = (1013, "") if limit == "send-timeout" else (1008, "token_expired")
                assert _assert_closed(ws, code).reason == reason
                assert gate.cancelled.wait(timeout=5)
                assert gate.application_done.wait(timeout=5)
                _assert_no_device_access(classic)
        finally:
            client.portal.call(gate.release.set)


@pytest.mark.parametrize("denial_extension", [True, False], ids=["http-denial", "close-fallback"])
def test_stalled_upgrade_denial_finishes_without_device_access(transport_service, monkeypatch, denial_extension):
    client, classic = transport_service
    monkeypatch.setattr(api, "WS_SEND_TIMEOUT", 0.05)
    monkeypatch.setattr(subscriptions, "WS_SEND_TIMEOUT", 0.05)

    async def denied_connection():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            expected = "websocket.http.response.start" if denial_extension else "websocket.close"
            assert message["type"] == expected
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/api/v1/ws",
            "raw_path": b"/api/v1/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", b"Bearer invalid")],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "subprotocols": [],
            "extensions": {"websocket.http.response": {}} if denial_extension else {},
            "state": {},
        }
        # A failed HTTP denial may already have changed Starlette's state, so
        # no final frame is promised. The ASGI request and blocked send must end.
        async with asyncio.timeout(2):
            await client.app(scope, receive, send)
        assert entered.is_set()
        assert cancelled.is_set()

    client.portal.call(denied_connection)
    _assert_no_device_access(classic)


def test_elapsed_browser_deadline_sends_no_acknowledgement(transport_service, entra, monkeypatch):
    client, classic = transport_service
    claims = entra.claims()
    token = signed_claims(entra, claims)
    monkeypatch.setattr(api, "time", lambda: claims["exp"] + JWT_LEEWAY_SECONDS)
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"op": "authenticate", "access_token": token})
        assert _assert_closed(ws, 1008).reason == "token_expired"
    _assert_no_device_access(classic)
