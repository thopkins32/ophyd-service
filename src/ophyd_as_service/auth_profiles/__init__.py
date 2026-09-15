"""Explicit composition of the supported access-token profile."""

from ..auth import JwtAuthenticator
from ..config import AuthConfig
from .entra import EntraProfile


def build_authenticator(config: AuthConfig) -> JwtAuthenticator:
    return JwtAuthenticator(EntraProfile(config.profile))
