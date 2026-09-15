"""Single-tenant Microsoft Entra access-token and reader policy."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from ..auth import JwtTrust
from ..config import EntraProfileConfig
from ..errors import auth_error

READER_ROLE = "Ophyd.Reader"
DELEGATED_READ_SCOPE = "Ophyd.Read"


def _guid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise auth_error("unauthenticated")
    try:
        return UUID(value)
    except ValueError:
        raise auth_error("unauthenticated") from None


class EntraProfile:
    def __init__(self, config: EntraProfileConfig) -> None:
        self._tenant_id = config.tenant_id
        tenant_id = str(config.tenant_id)
        issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self.trust = JwtTrust(
            issuer=issuer,
            audience=str(config.api_client_id),
            discovery_url=f"{issuer}/.well-known/openid-configuration",
            jwks_origins=frozenset({("login.microsoftonline.com", 443)}),
            additional_required_claims=("iat", "nbf", "tid", "ver", "idtyp", "oid", "azp"),
        )

    def accepts_signing_key(self, jwk: Mapping[str, Any]) -> bool:
        if "issuer" not in jwk:
            return True
        issuer = jwk["issuer"]
        if not isinstance(issuer, str):
            raise auth_error("auth_unavailable")
        return issuer.replace("{tenantid}", str(self._tenant_id)) == self.trust.issuer

    def validate_access_token(self, *, header: Mapping[str, Any], claims: Mapping[str, Any]) -> None:
        if claims.get("ver") != "2.0" or _guid(claims.get("tid")) != self._tenant_id:
            raise auth_error("unauthenticated")
        _guid(claims.get("oid"))
        _guid(claims.get("azp"))
        if claims.get("idtyp") not in ("user", "app"):
            raise auth_error("unauthenticated")
        if "roles" in claims:
            roles = claims["roles"]
            if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
                raise auth_error("unauthenticated")
        if "scp" in claims:
            if claims["idtyp"] == "app" or not isinstance(claims["scp"], str):
                raise auth_error("unauthenticated")

    def require_reader(self, claims: Mapping[str, Any]) -> None:
        if READER_ROLE not in claims.get("roles", ()):
            raise auth_error("forbidden")
        if claims["idtyp"] == "user" and DELEGATED_READ_SCOPE not in claims.get("scp", "").split(" "):
            raise auth_error("forbidden")
