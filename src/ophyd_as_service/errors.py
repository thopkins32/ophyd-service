"""Service failures safe to expose across transports without native SDK imports."""

from typing import Literal


class ServiceError(Exception):
    """An operation failure safe to expose without a server traceback."""

    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    @property
    def status(self) -> int:
        return {
            "unauthenticated": 401,
            "forbidden": 403,
            "auth_unavailable": 503,
            "not_found": 404,
            "not_readable": 409,
            "not_monitorable": 409,
            "timeout": 504,
            "backend_error": 502,
            "serialization_error": 500,
        }[self.code]


_AUTH_MESSAGES = {
    "unauthenticated": "A valid access token is required",
    "forbidden": "Reader access is required",
    "auth_unavailable": "Signing keys are temporarily unavailable",
}


def auth_error(code: Literal["unauthenticated", "forbidden", "auth_unavailable"]) -> ServiceError:
    return ServiceError(code, _AUTH_MESSAGES[code])
