import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def jwt_signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "test-rsa-signing-key"
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(kid=kid, use="sig", alg="RS256")
    return SimpleNamespace(private_key=private_key, kid=kid, public_jwk=public_jwk)


@pytest.fixture
def entra(jwt_signing_key, respx_mock):
    tenant_id = "11111111-1111-4111-8111-111111111111"
    api_client_id = "22222222-2222-4222-8222-222222222222"
    object_id = "33333333-3333-4333-8333-333333333333"
    client_id = "44444444-4444-4444-8444-444444444444"
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    issuer = f"{authority}/v2.0"
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    jwks_url = f"{authority}/discovery/v2.0/keys"
    auth = {
        "profile": {"type": "entra", "tenant_id": tenant_id, "api_client_id": api_client_id},
        "allowed_origins": [],
    }
    toml = (
        'auth = {profile = {type = "entra", '
        f'tenant_id = "{tenant_id}", api_client_id = "{api_client_id}"'
        "}, allowed_origins = []}\n"
    )

    def default_claims(*, kind="user"):
        if kind not in {"user", "app"}:
            raise ValueError(f"Unknown test access-token kind: {kind}")
        now = int(time.time())
        payload = {
            "iss": issuer,
            "aud": api_client_id,
            "sub": object_id,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "tid": tenant_id,
            "ver": "2.0",
            "idtyp": kind,
            "oid": object_id,
            "azp": client_id,
            "roles": ["Ophyd.Reader"],
        }
        if kind == "user":
            payload["scp"] = "Ophyd.Read"
        return payload

    def token(*, kind="user", claims=None):
        payload = default_claims(kind=kind)
        if claims is not None:
            payload.update(claims)
        return jwt.encode(
            payload,
            jwt_signing_key.private_key,
            algorithm="RS256",
            headers={"kid": jwt_signing_key.kid},
        )

    # Configure only this fixture's router; unused discovery is valid for config
    # and direct-registry tests, but every unmatched HTTPX request still fails.
    with respx_mock(assert_all_called=False, assert_all_mocked=True) as router:
        discovery_route = router.get(discovery_url).respond(
            200,
            json={
                "issuer": issuer,
                "jwks_uri": jwks_url,
                "authorization_endpoint": f"{authority}/oauth2/v2.0/authorize",
                "token_endpoint": f"{authority}/oauth2/v2.0/token",
                "response_types_supported": ["code", "id_token", "code id_token"],
                "subject_types_supported": ["pairwise"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post", "private_key_jwt"],
            },
        )
        jwks_route = router.get(jwks_url).respond(
            200,
            json={
                "keys": [
                    {
                        **jwt_signing_key.public_jwk,
                        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
                    }
                ]
            },
        )
        yield SimpleNamespace(
            auth=auth,
            toml=toml,
            headers={"Authorization": f"Bearer {token()}"},
            tenant_id=tenant_id,
            api_client_id=api_client_id,
            object_id=object_id,
            client_id=client_id,
            issuer=issuer,
            discovery_url=discovery_url,
            jwks_url=jwks_url,
            signing_key=jwt_signing_key,
            discovery_route=discovery_route,
            jwks_route=jwks_route,
            claims=default_claims,
            token=token,
        )
