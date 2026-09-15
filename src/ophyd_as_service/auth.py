"""RS256 access-token admission with instance-owned OIDC trust and key caching."""

import asyncio
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import jwt

from .errors import ServiceError, auth_error

JWT_LEEWAY_SECONDS = 30
MAX_ACCESS_TOKEN_BYTES = 65536
OIDC_REQUEST_TIMEOUT_SECONDS = 5.0
OIDC_REFRESH_TIMEOUT_SECONDS = 10.0
MAX_OIDC_RESPONSE_BYTES = 2097152
KEY_REFRESH_SECONDS = 3600
KEY_MAX_AGE_SECONDS = 86400
KEY_REFRESH_MIN_INTERVAL_SECONDS = 300

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    expires_at: float


@dataclass(frozen=True)
class JwtTrust:
    issuer: str
    audience: str
    discovery_url: str
    jwks_origins: frozenset[tuple[str, int]]
    additional_required_claims: tuple[str, ...] = ()


class AccessTokenProfile(Protocol):
    """Pure, rejection-only policy over keys and cryptographically verified tokens.

    Hooks must not mutate the mappings or perform I/O. A profile cannot replace
    common verification, signing material, endpoints, or the resulting principal.
    """

    trust: JwtTrust

    def accepts_signing_key(self, jwk: Mapping[str, Any]) -> bool: ...

    def validate_access_token(self, *, header: Mapping[str, Any], claims: Mapping[str, Any]) -> None: ...

    def require_reader(self, claims: Mapping[str, Any]) -> None: ...


def _https_origin(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    port = parsed.port
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)
        or (port is None and parsed.netloc.endswith(":"))
    ):
        raise ValueError("Signing endpoints must be absolute HTTPS URLs without userinfo or fragments")
    return parsed.hostname.lower(), 443 if port is None else port


def _report_background_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        # Retrieve failures even after a requesting waiter is cancelled. Do not
        # log foreign documents, credentials, or exception messages containing them.
        logger.warning("Authentication background operation failed (%s)", type(error).__name__)


class JwtAuthenticator:
    """One lifespan owns one immutable trust description, HTTP client and cache."""

    def __init__(self, profile: AccessTokenProfile):
        self._profile = profile
        self._trust = profile.trust
        if not self._trust.issuer or not self._trust.audience or not self._trust.jwks_origins:
            raise ValueError("JWT trust requires an issuer, audience and signing-key origin allowlist")
        _https_origin(self._trust.discovery_url)
        self._options = {
            "verify_signature": True,
            "verify_iss": True,
            "verify_aud": True,
            "verify_exp": True,
            "verify_sub": True,
            "verify_iat": True,
            "verify_nbf": True,
            "strict_aud": True,
            "enforce_minimum_key_length": True,
            "require": ("iss", "aud", "exp", "sub", *self._trust.additional_required_claims),
        }
        self._keys: dict[str, jwt.PyJWK] = {}
        self._last_attempt: float | None = None
        self._last_success: float | None = None
        self._refresh_failed = False
        self._refresh_task: asyncio.Task | None = None
        self._periodic_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("An authenticator can only be started once")
        self._client = httpx.AsyncClient(timeout=OIDC_REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
        self._started = True
        try:
            await asyncio.shield(self._refresh())
            if not self._usable_keys():
                raise auth_error("auth_unavailable")
        except BaseException:
            await self.close()
            raise
        self._periodic_task = asyncio.create_task(self._refresh_periodically())
        self._periodic_task.add_done_callback(_report_background_failure)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(_report_background_failure)
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        tasks = [task for task in (self._periodic_task, self._refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._keys.clear()
        if self._started:
            await self._client.aclose()

    async def require_reader(self, access_token: str) -> Principal:
        try:
            if (
                len(access_token) > MAX_ACCESS_TOKEN_BYTES
                or len(access_token.encode("utf-8")) > MAX_ACCESS_TOKEN_BYTES
            ):
                raise auth_error("unauthenticated")
            segments = access_token.split(".")
            if len(segments) != 3 or not all(segments):
                raise auth_error("unauthenticated")
            header = jwt.get_unverified_header(access_token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid or header.get("b64") is False:
                raise auth_error("unauthenticated")
        except (jwt.InvalidTokenError, TypeError, ValueError, RecursionError) as exc:
            raise auth_error("unauthenticated") from exc

        key = await self._signing_key(kid)
        try:
            verified = jwt.decode_complete(
                access_token,
                key=key,
                algorithms=["RS256"],
                issuer=self._trust.issuer,
                audience=self._trust.audience,
                leeway=JWT_LEEWAY_SECONDS,
                options=self._options,
            )
            claims = verified["payload"]
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise auth_error("unauthenticated")
            for claim in ("exp", "iat", "nbf"):
                if claim in claims:
                    value = claims[claim]
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise auth_error("unauthenticated")
            principal = Principal(self._trust.issuer, claims["sub"], claims["exp"] + JWT_LEEWAY_SECONDS)
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            # These exceptions arise from the foreign JWT representation. Keep
            # profile/programming errors outside this normalization boundary.
            raise auth_error("unauthenticated") from exc

        self._profile.validate_access_token(header=verified["header"], claims=claims)
        self._profile.require_reader(claims)
        return principal

    def _usable_keys(self) -> bool:
        return self._last_success is not None and monotonic() - self._last_success < KEY_MAX_AGE_SECONDS

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        if not self._started or self._closed:
            raise auth_error("auth_unavailable")
        if self._usable_keys() and kid in self._keys:
            return self._keys[kid]
        await asyncio.shield(self._refresh())
        if self._closed or not self._usable_keys():
            raise auth_error("auth_unavailable")
        if kid in self._keys:
            return self._keys[kid]
        raise auth_error("auth_unavailable" if self._refresh_failed else "unauthenticated")

    def _refresh(self) -> asyncio.Task:
        now = monotonic()
        if self._refresh_task is not None and (
            not self._refresh_task.done()
            or (self._last_attempt is not None and now - self._last_attempt < KEY_REFRESH_MIN_INTERVAL_SECONDS)
        ):
            return self._refresh_task
        self._last_attempt = now
        self._refresh_task = asyncio.create_task(self._refresh_keys())
        self._refresh_task.add_done_callback(_report_background_failure)
        return self._refresh_task

    async def _refresh_periodically(self) -> None:
        while True:
            await asyncio.sleep(KEY_REFRESH_SECONDS)
            await asyncio.shield(self._refresh())

    async def _document(self, url: str) -> Any:
        try:
            async with self._client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_OIDC_RESPONSE_BYTES:
                        raise auth_error("auth_unavailable")
                    body.extend(chunk)
                return json.loads(body)
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise auth_error("auth_unavailable") from exc

    async def _refresh_keys(self) -> None:
        try:
            async with asyncio.timeout(OIDC_REFRESH_TIMEOUT_SECONDS):
                discovery = await self._document(self._trust.discovery_url)
                if (
                    not isinstance(discovery, dict)
                    or discovery.get("issuer") != self._trust.issuer
                    or not isinstance(discovery.get("jwks_uri"), str)
                ):
                    raise auth_error("auth_unavailable")
                jwks_url = discovery["jwks_uri"]
                try:
                    origin = _https_origin(jwks_url)
                except ValueError as exc:
                    raise auth_error("auth_unavailable") from exc
                if origin not in self._trust.jwks_origins:
                    raise auth_error("auth_unavailable")
                keys = self._parse_keys(await self._document(jwks_url))
        except (TimeoutError, ServiceError) as exc:
            # Keep last-known-good keys and their age; report each failed attempt
            # once here, not once per request waiting on the shared refresh.
            self._refresh_failed = True
            logger.warning("Signing key refresh failed (%s)", type(exc).__name__)
        else:
            self._keys = keys
            self._last_success = monotonic()
            self._refresh_failed = False

    def _parse_keys(self, document: Any) -> dict[str, jwt.PyJWK]:
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list) or not document["keys"]:
            raise auth_error("auth_unavailable")
        keys = {}
        for jwk in document["keys"]:
            if not isinstance(jwk, dict):
                raise auth_error("auth_unavailable")
            kid = jwk.get("kid")
            if (
                jwk.get("kty") != "RSA"
                or not isinstance(kid, str)
                or not kid
                or ("use" in jwk and jwk["use"] != "sig")
                or ("alg" in jwk and jwk["alg"] != "RS256")
            ):
                continue
            if "key_ops" in jwk:
                operations = jwk["key_ops"]
                if not isinstance(operations, list) or any(
                    not isinstance(operation, str) for operation in operations
                ):
                    raise auth_error("auth_unavailable")
                if "verify" not in operations:
                    continue
            if not self._profile.accepts_signing_key(jwk):
                continue
            if kid in keys:
                raise auth_error("auth_unavailable")
            try:
                key = jwt.PyJWK.from_dict(jwk, algorithm="RS256")
                if key.Algorithm.check_key_length(key.key):
                    raise auth_error("auth_unavailable")
            except (jwt.PyJWTError, TypeError, ValueError, KeyError, OverflowError) as exc:
                raise auth_error("auth_unavailable") from exc
            keys[kid] = key
        if not keys:
            raise auth_error("auth_unavailable")
        return keys
