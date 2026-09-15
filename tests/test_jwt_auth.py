import asyncio
import json
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ophyd_as_service import auth
from ophyd_as_service.auth import JwtAuthenticator, JwtTrust, Principal
from ophyd_as_service.errors import ServiceError, auth_error

pytestmark = pytest.mark.asyncio


class ExampleAccessTokenProfile:
    """Test-only policy deliberately independent of Entra's identity vocabulary."""

    def __init__(self, trust=None):
        self.trust = trust or JwtTrust(
            issuer="https://issuer.example.test",
            audience="telemetry-api",
            discovery_url="https://issuer.example.test/.well-known/openid-configuration",
            jwks_origins=frozenset({("keys.example.test", 443)}),
        )

    def accepts_signing_key(self, jwk: Mapping[str, Any]) -> bool:
        realm = jwk.get("test_realm")
        if not isinstance(realm, str):
            raise auth_error("auth_unavailable")
        return realm == "reader-keys"

    def validate_access_token(self, *, header: Mapping[str, Any], claims: Mapping[str, Any]) -> None:
        if header.get("typ") != "at+jwt":
            raise auth_error("unauthenticated")
        if "permissions" in claims:
            permissions = claims["permissions"]
            if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
                raise auth_error("unauthenticated")

    def require_reader(self, claims: Mapping[str, Any]) -> None:
        if "read" not in claims.get("permissions", []):
            raise auth_error("forbidden")


@pytest.fixture(scope="session")
def alternate_signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(kid="rotated-key", use="sig", alg="RS256")
    return SimpleNamespace(private_key=private_key, kid="rotated-key", public_jwk=public_jwk)


def _marked_key(key, **extensions):
    return {**key.public_jwk, "test_realm": "reader-keys", **extensions}


def _sign(key, claims, *, headers=None, algorithm="RS256"):
    return jwt.encode(
        claims,
        key.private_key,
        algorithm=algorithm,
        headers={"kid": key.kid, "typ": "at+jwt", **(headers or {})},
    )


@pytest.fixture
def example(jwt_signing_key, respx_mock, monkeypatch):
    profile = ExampleAccessTokenProfile()
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(auth, "monotonic", lambda: clock.now)
    # Query and non-root path must be preserved, not reconstructed from the origin.
    jwks_url = "https://keys.example.test/signing/current?version=2"

    def claims(**overrides):
        return {
            "iss": profile.trust.issuer,
            "aud": profile.trust.audience,
            "sub": "collector:west/17",
            "exp": time.time() + 3600,
            "permissions": ["read"],
            **overrides,
        }

    def token(*, key=jwt_signing_key, headers=None, **overrides):
        return _sign(key, claims(**overrides), headers=headers)

    with respx_mock(assert_all_called=False, assert_all_mocked=True) as router:
        discovery = router.get(profile.trust.discovery_url).respond(
            200, json={"issuer": profile.trust.issuer, "jwks_uri": jwks_url}
        )
        jwks = router.get(jwks_url).respond(200, json={"keys": [_marked_key(jwt_signing_key)]})
        yield SimpleNamespace(
            profile=profile,
            authenticator=JwtAuthenticator(profile),
            key=jwt_signing_key,
            clock=clock,
            router=router,
            discovery=discovery,
            jwks=jwks,
            jwks_url=jwks_url,
            claims=claims,
            token=token,
        )


@asynccontextmanager
async def _started(authenticator):
    try:
        await authenticator.start()
        yield authenticator
    finally:
        await authenticator.close()


@contextmanager
def _denied(code):
    with pytest.raises(ServiceError) as caught:
        yield
    assert caught.value.code == code
    assert caught.value.status == {"unauthenticated": 401, "forbidden": 403, "auth_unavailable": 503}[code]
    assert caught.value.path is None


async def test_provider_neutral_access_token_accepts_opaque_subject_and_common_claims_only(example):
    claims = example.claims()
    async with _started(example.authenticator) as authenticator:
        principal = await authenticator.require_reader(_sign(example.key, claims))
    assert principal == Principal(
        issuer="https://issuer.example.test",
        subject="collector:west/17",
        expires_at=claims["exp"] + auth.JWT_LEEWAY_SECONDS,
    )


@pytest.mark.parametrize("claim", ["iat", "nbf"])
async def test_optional_registered_dates_are_verified_when_present(example, claim):
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token(**{claim: time.time() + 300}))


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "sub"])
async def test_common_claims_are_required(example, claim):
    claims = example.claims()
    del claims[claim]
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(_sign(example.key, claims))


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("sub", ""),
        ("sub", 17),
        ("sub", []),
        ("exp", "4000000000"),
        ("exp", None),
        ("exp", []),
        ("exp", {}),
        ("exp", float("nan")),
        ("exp", float("inf")),
        ("iat", "1"),
        ("iat", False),
        ("iat", None),
        ("iat", {}),
        ("iat", float("-inf")),
        ("nbf", "1"),
        ("nbf", False),
        ("nbf", None),
        ("nbf", []),
        ("nbf", float("nan")),
    ],
)
async def test_claim_representations_are_not_coerced_or_leaked_as_python_errors(example, claim, value):
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token(**{claim: value}))


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://untrusted.example.test"),
        ("iss", ["https://issuer.example.test"]),
        ("aud", "another-api"),
        ("aud", ["telemetry-api"]),
        ("aud", {"api": "telemetry-api"}),
    ],
)
async def test_issuer_and_audience_are_exact_single_trusted_values(example, claim, value):
    # JWT encoding rejects non-string issuers before signing; the JWS API lets
    # this test present an actually signed foreign JSON representation.
    token = jwt.api_jws.encode(
        json.dumps(example.claims(**{claim: value})).encode("utf-8"),
        example.key.private_key,
        algorithm="RS256",
        headers={"kid": example.key.kid, "typ": "at+jwt"},
    )
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(token)


async def test_expiry_uses_real_fixed_clock_tolerance(example):
    now = time.time()
    async with _started(example.authenticator) as authenticator:
        principal = await authenticator.require_reader(example.token(exp=now - 20))
        assert principal.expires_at == now - 20 + 30
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token(exp=now - 40))


@pytest.mark.parametrize("marker", ["JWT", None])
async def test_profile_access_token_marker_is_mandatory_even_for_a_signed_reader(example, marker):
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token(headers={"typ": marker}))


@pytest.mark.parametrize(
    ("permissions", "code"),
    [([], "forbidden"), (["write"], "forbidden"), ("read", "unauthenticated"), (["read", 17], "unauthenticated")],
)
async def test_profile_distinguishes_missing_reader_permission_from_malformed_permissions(
    example, permissions, code
):
    async with _started(example.authenticator) as authenticator:
        with _denied(code):
            await authenticator.require_reader(example.token(permissions=permissions))


async def test_entra_looking_roles_do_not_grant_example_profile_reader_access(example):
    claims = example.claims(roles=["Ophyd.Reader"], scp="Ophyd.Read", idtyp="user")
    del claims["permissions"]
    async with _started(example.authenticator) as authenticator:
        with _denied("forbidden"):
            await authenticator.require_reader(_sign(example.key, claims))


@pytest.mark.parametrize("algorithm", ["none", "HS256", "RS384"])
async def test_disallowed_algorithms_never_trigger_key_refresh(example, algorithm):
    key = {"none": None, "HS256": "not-a-public-key-but-a-long-hmac-secret", "RS384": example.key.private_key}[
        algorithm
    ]
    token = jwt.encode(example.claims(), key, algorithm=algorithm, headers={"kid": "unknown", "typ": "at+jwt"})
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("unauthenticated"):
            await authenticator.require_reader(token)
        assert example.discovery.call_count == 1
        assert example.jwks.call_count == 1


@pytest.mark.parametrize("kind", ["missing-kid", "compact", "header-json", "oversized"])
async def test_structurally_invalid_tokens_never_trigger_key_refresh(example, kind):
    tokens = {
        "missing-kid": jwt.encode(
            example.claims(), example.key.private_key, algorithm="RS256", headers={"typ": "at+jwt"}
        ),
        "compact": "one.two.three.four",
        "header-json": "W10.e30.AA",
        "oversized": "x" * (auth.MAX_ACCESS_TOKEN_BYTES + 1),
    }
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("unauthenticated"):
            await authenticator.require_reader(tokens[kind])
        assert example.discovery.call_count == 1
        assert example.jwks.call_count == 1


async def test_invalid_signature_under_known_key_never_triggers_refresh(example, alternate_signing_key):
    token = _sign(alternate_signing_key, example.claims(), headers={"kid": example.key.kid})
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("unauthenticated"):
            await authenticator.require_reader(token)
        assert example.discovery.call_count == 1
        assert example.jwks.call_count == 1


async def test_token_headers_and_issuer_cannot_select_key_endpoints(example):
    headers = {"jku": "https://attacker.example.test/jwks", "x5u": "https://attacker.example.test/certificate"}
    async with _started(example.authenticator) as authenticator:
        principal = await authenticator.require_reader(example.token(headers=headers))
        assert principal.subject == "collector:west/17"
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token(headers=headers, iss="https://attacker.example.test"))
        assert example.discovery.call_count == 1
        assert example.jwks.call_count == 1
        for call in example.router.calls:
            assert "authorization" not in call.request.headers


async def test_instances_with_the_same_kid_do_not_share_issuer_or_key_trust(example, alternate_signing_key):
    other_trust = JwtTrust(
        issuer="https://other-issuer.example.test",
        audience="other-api",
        discovery_url="https://other-issuer.example.test/.well-known/openid-configuration",
        jwks_origins=frozenset({("other-keys.example.test", 443)}),
    )
    other_keys_url = "https://other-keys.example.test/jwks"
    example.router.get(other_trust.discovery_url).respond(
        200, json={"issuer": other_trust.issuer, "jwks_uri": other_keys_url}
    )
    example.router.get(other_keys_url).respond(
        200, json={"keys": [_marked_key(alternate_signing_key, kid=example.key.kid)]}
    )
    other = JwtAuthenticator(ExampleAccessTokenProfile(other_trust))
    other_claims = example.claims(iss=other_trust.issuer, aud=other_trust.audience)
    other_token = _sign(alternate_signing_key, other_claims, headers={"kid": example.key.kid})
    async with _started(example.authenticator) as first, _started(other) as second:
        assert (await first.require_reader(example.token())).issuer == example.profile.trust.issuer
        assert (await second.require_reader(other_token)).issuer == other_trust.issuer
        with _denied("unauthenticated"):
            await first.require_reader(
                _sign(alternate_signing_key, example.claims(), headers={"kid": example.key.kid})
            )
        with _denied("unauthenticated"):
            await second.require_reader(_sign(example.key, other_claims))
        with _denied("unauthenticated"):
            await first.require_reader(other_token)


async def test_profile_filters_signing_keys_without_interpreting_microsoft_extensions(
    example, alternate_signing_key
):
    example.jwks.respond(
        200,
        json={
            "keys": [
                _marked_key(example.key, test_realm="another-realm"),
                _marked_key(alternate_signing_key, issuer={"not": "a Microsoft issuer template"}),
            ]
        },
    )
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token())
        principal = await authenticator.require_reader(example.token(key=alternate_signing_key))
        assert principal.subject == "collector:west/17"


async def test_profile_additional_required_claim_is_enforced_by_common_verification(example):
    profile = ExampleAccessTokenProfile(replace(example.profile.trust, additional_required_claims=("context",)))
    async with _started(JwtAuthenticator(profile)) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(example.token())
        principal = await authenticator.require_reader(example.token(context="probe"))
        assert principal.subject == "collector:west/17"


async def test_successful_rotation_retires_removed_keys_atomically(example, alternate_signing_key):
    async with _started(example.authenticator) as authenticator:
        original = example.token()
        assert (await authenticator.require_reader(original)).subject == "collector:west/17"
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.jwks.respond(200, json={"keys": [_marked_key(alternate_signing_key)]})
        rotated = example.token(key=alternate_signing_key)
        assert (await authenticator.require_reader(rotated)).subject == "collector:west/17"
        with _denied("unauthenticated"):
            await authenticator.require_reader(original)
        assert (await authenticator.require_reader(rotated)).subject == "collector:west/17"
        assert example.discovery.call_count == 2
        assert example.jwks.call_count == 2


async def test_duplicate_rotation_cannot_publish_a_partial_new_key_set(example, alternate_signing_key):
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        rotated_key = _marked_key(alternate_signing_key)
        example.jwks.respond(200, json={"keys": [rotated_key, rotated_key]})
        rotated = example.token(key=alternate_signing_key)
        with _denied("auth_unavailable"):
            await authenticator.require_reader(rotated)
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"
        with _denied("auth_unavailable"):
            await authenticator.require_reader(rotated)
        assert example.jwks.call_count == 2


async def test_unknown_key_after_successful_refresh_is_401_and_obeys_cooldown(example, alternate_signing_key):
    unknown = example.token(key=alternate_signing_key)
    async with _started(example.authenticator) as authenticator:
        with _denied("unauthenticated"):
            await authenticator.require_reader(unknown)
        assert example.discovery.call_count == 1
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("unauthenticated"):
            await authenticator.require_reader(unknown)
        with _denied("unauthenticated"):
            await authenticator.require_reader(unknown)
        assert example.discovery.call_count == 2
        assert example.jwks.call_count == 2


async def test_failed_refresh_cooldown_is_measured_from_attempt_not_last_success(example, alternate_signing_key):
    unknown = example.token(key=alternate_signing_key)
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.discovery.respond(503)
        with _denied("auth_unavailable"):
            await authenticator.require_reader(unknown)
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS - 1
        with _denied("auth_unavailable"):
            await authenticator.require_reader(unknown)
        assert example.discovery.call_count == 2
        example.clock.now += 1
        example.discovery.respond(200, json={"issuer": example.profile.trust.issuer, "jwks_uri": example.jwks_url})
        example.jwks.respond(200, json={"keys": [_marked_key(example.key), _marked_key(alternate_signing_key)]})
        assert (await authenticator.require_reader(unknown)).subject == "collector:west/17"
        assert example.discovery.call_count == 3


async def test_failed_refresh_does_not_extend_last_known_good_key_age(example, alternate_signing_key):
    async with _started(example.authenticator) as authenticator:
        example.discovery.respond(503)
        example.clock.now += auth.KEY_MAX_AGE_SECONDS - 1
        with _denied("auth_unavailable"):
            await authenticator.require_reader(example.token(key=alternate_signing_key))
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"
        example.clock.now += 1
        with _denied("auth_unavailable"):
            await authenticator.require_reader(example.token())
        assert example.discovery.call_count == 2


async def test_concurrent_unknown_keys_share_refresh_and_one_cancelled_waiter_cannot_cancel_it(
    example, alternate_signing_key
):
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def rotated_keys(request):
        entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"keys": [_marked_key(alternate_signing_key)]})
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.jwks.mock(side_effect=rotated_keys)
        token = example.token(key=alternate_signing_key)
        abandoned = asyncio.create_task(authenticator.require_reader(token))
        survivors = []
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            started = [asyncio.Event() for _ in range(3)]

            async def admit(event):
                event.set()
                return await authenticator.require_reader(token)

            survivors = [asyncio.create_task(admit(event)) for event in started]
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=2)
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
            assert not cancelled.is_set()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*survivors), timeout=2)
            assert all(principal.subject == "collector:west/17" for principal in results)
            assert example.discovery.call_count == 2
            assert example.jwks.call_count == 2
        finally:
            release.set()
            for task in [abandoned, *survivors]:
                task.cancel()
            await asyncio.gather(abandoned, *survivors, return_exceptions=True)


async def test_periodic_refresh_discovers_rotated_keys_without_a_request_trigger(
    example, alternate_signing_key, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def rotated_keys(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"keys": [_marked_key(alternate_signing_key)]})

    monkeypatch.setattr(auth, "KEY_REFRESH_SECONDS", 0.01)
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.jwks.mock(side_effect=rotated_keys)
        rotated = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            rotated = asyncio.create_task(authenticator.require_reader(example.token(key=alternate_signing_key)))
            release.set()
            principal = await asyncio.wait_for(rotated, timeout=2)
            assert principal.subject == "collector:west/17"
            with _denied("unauthenticated"):
                await authenticator.require_reader(example.token())
            assert example.discovery.call_count == 2
            assert example.jwks.call_count == 2
        finally:
            release.set()
            if rotated is not None:
                rotated.cancel()
                await asyncio.gather(rotated, return_exceptions=True)


async def test_close_cancels_and_awaits_owned_refresh_http_work(example, alternate_signing_key):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_keys(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    authenticator = example.authenticator
    waiter = None
    try:
        await authenticator.start()
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.jwks.mock(side_effect=blocked_keys)
        waiter = asyncio.create_task(authenticator.require_reader(example.token(key=alternate_signing_key)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(authenticator.close(), timeout=2)
        assert cancelled.is_set()
        outcome = (await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=2))[0]
        assert isinstance(outcome, (asyncio.CancelledError, ServiceError))
        if isinstance(outcome, ServiceError):
            assert outcome.code == "auth_unavailable"
        await authenticator.close()
    finally:
        await authenticator.close()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def _break_provider(example, failure):
    valid = _marked_key(example.key)
    if failure == "discovery-redirect":
        example.discovery.respond(302, headers={"location": "https://redirect.example.test/discovery"})
    elif failure == "jwks-redirect":
        example.jwks.respond(307, headers={"location": "https://redirect.example.test/keys"})
    elif failure == "network-error":
        example.jwks.mock(side_effect=httpx.ConnectError("Offline signing-key endpoint"))
    elif failure == "metadata-issuer":
        example.discovery.respond(200, json={"issuer": "https://wrong.example.test", "jwks_uri": example.jwks_url})
    elif failure == "metadata-shape":
        example.discovery.respond(200, json=[])
    elif failure == "metadata-types":
        example.discovery.respond(200, json={"issuer": example.profile.trust.issuer, "jwks_uri": 42})
    elif failure == "discovery-json":
        example.discovery.respond(200, content=b"{broken")
    elif failure == "jwks-json":
        example.jwks.respond(200, content=b"{broken")
    elif failure == "jwks-shape":
        example.jwks.respond(200, json=[])
    elif failure == "empty-keys":
        example.jwks.respond(200, json={"keys": []})
    elif failure == "nonobject-key":
        example.jwks.respond(200, json={"keys": [valid, None]})
    elif failure == "malformed-rsa":
        example.jwks.respond(200, json={"keys": [valid, {**valid, "kid": "broken", "n": "!"}]})
    elif failure == "key-ops-string":
        example.jwks.respond(200, json={"keys": [valid, {**valid, "kid": "broken", "key_ops": "verify"}]})
    elif failure == "key-ops-item":
        example.jwks.respond(200, json={"keys": [valid, {**valid, "kid": "broken", "key_ops": ["verify", 42]}]})
    elif failure == "profile-key-metadata":
        example.jwks.respond(200, json={"keys": [valid, {**valid, "kid": "broken", "test_realm": []}]})
    elif failure == "no-usable-keys":
        example.jwks.respond(200, json={"keys": [{**valid, "test_realm": "another-realm"}]})
    else:
        raise AssertionError(f"Unknown provider failure fixture: {failure}")


@pytest.mark.parametrize(
    "failure",
    [
        "discovery-redirect",
        "jwks-redirect",
        "network-error",
        "metadata-issuer",
        "metadata-shape",
        "metadata-types",
        "discovery-json",
        "jwks-json",
        "jwks-shape",
        "empty-keys",
        "nonobject-key",
        "malformed-rsa",
        "key-ops-string",
        "key-ops-item",
        "profile-key-metadata",
        "no-usable-keys",
    ],
)
async def test_invalid_refresh_fails_closed_without_replacing_last_known_good_keys(
    example, alternate_signing_key, failure
):
    async with _started(example.authenticator) as authenticator:
        _break_provider(example, failure)
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("auth_unavailable"):
            await authenticator.require_reader(example.token(key=alternate_signing_key))
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"


@pytest.mark.parametrize(
    "jwks_url",
    [
        "http://keys.example.test/signing/current",
        "https://browser-only.example.test/jwks",
        "https://keys.example.test:444/jwks",
        "https://keys.example.test:0/jwks",
        "https://user:password@keys.example.test/jwks",
        "https://keys.example.test/jwks#fragment",
    ],
)
async def test_discovery_cannot_widen_jwks_origin_or_url_trust(example, alternate_signing_key, jwks_url):
    async with _started(example.authenticator) as authenticator:
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.discovery.respond(200, json={"issuer": example.profile.trust.issuer, "jwks_uri": jwks_url})
        with _denied("auth_unavailable"):
            await authenticator.require_reader(example.token(key=alternate_signing_key))
        assert example.jwks.call_count == 1
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
async def test_streamed_response_size_cap_closes_body_and_preserves_good_keys(
    example, alternate_signing_key, endpoint
):
    document = (
        {"issuer": example.profile.trust.issuer, "jwks_uri": example.jwks_url}
        if endpoint == "discovery"
        else {"keys": [_marked_key(example.key)]}
    )
    document["padding"] = "x" * auth.MAX_OIDC_RESPONSE_BYTES
    body = json.dumps(document).encode()
    stream = _ChunkedBody([body[: auth.MAX_OIDC_RESPONSE_BYTES], body[auth.MAX_OIDC_RESPONSE_BYTES :]])
    async with _started(example.authenticator) as authenticator:
        getattr(example, endpoint).mock(return_value=httpx.Response(200, stream=stream))
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        with _denied("auth_unavailable"):
            await authenticator.require_reader(example.token(key=alternate_signing_key))
        assert stream.closed
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"


@pytest.mark.parametrize("endpoint", ["discovery", "jwks"])
async def test_initial_provider_failure_is_503_without_usable_trust(example, endpoint):
    getattr(example, endpoint).respond(503)
    with _denied("auth_unavailable"):
        async with _started(example.authenticator):
            pytest.fail("Startup must not succeed without trusted signing keys")


async def test_standard_ineligible_keys_are_skipped_before_profile_policy(example):
    valid = _marked_key(example.key, key_ops=["verify"])
    # Invalid profile extensions on ineligible keys must never reach its hook.
    ineligible = [
        {"kty": "EC", "kid": "ec", "test_realm": []},
        {**example.key.public_jwk, "kid": "encryption", "use": "enc", "test_realm": []},
        {**example.key.public_jwk, "kid": "wrong-alg", "alg": "RS384", "test_realm": []},
        {**example.key.public_jwk, "kid": "sign-only", "key_ops": ["sign"], "test_realm": []},
    ]
    example.jwks.respond(200, json={"keys": [*ineligible, valid]})
    async with _started(example.authenticator) as authenticator:
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"


async def test_refresh_has_a_whole_operation_deadline_and_closes_inflight_body(
    example, alternate_signing_key, monkeypatch
):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    async with _started(example.authenticator) as authenticator:
        monkeypatch.setattr(auth, "OIDC_REFRESH_TIMEOUT_SECONDS", 0.05)
        example.clock.now += auth.KEY_REFRESH_MIN_INTERVAL_SECONDS
        example.jwks.mock(return_value=httpx.Response(200, stream=BlockedBody()))
        with _denied("auth_unavailable"):
            await asyncio.wait_for(
                authenticator.require_reader(example.token(key=alternate_signing_key)), timeout=2
            )
        assert entered.is_set()
        assert closed.is_set()
        assert (await authenticator.require_reader(example.token())).subject == "collector:west/17"


async def test_rejected_tokens_and_claims_are_not_logged_or_returned(example, caplog):
    marker = "private-claim-value-never-for-diagnostics"
    token = example.token(permissions="read", private_marker=marker)
    async with _started(example.authenticator) as authenticator:
        with pytest.raises(ServiceError) as caught:
            await authenticator.require_reader(token)
    assert caught.value.code == "unauthenticated"
    assert token not in caplog.text
    assert marker not in caplog.text
    assert token not in str(caught.value)
    assert marker not in str(caught.value)


async def test_profile_programming_errors_are_not_key_service_outages(example, monkeypatch):
    failure = RuntimeError("Broken profile policy")

    def broken_key_policy(jwk):
        raise failure

    monkeypatch.setattr(example.profile, "accepts_signing_key", broken_key_policy)
    with pytest.raises(RuntimeError) as caught:
        async with _started(example.authenticator):
            pytest.fail("A broken signing-key policy became ready")
    assert caught.value is failure
