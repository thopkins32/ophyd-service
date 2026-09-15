"""Trusted, data-only device construction configuration."""

import importlib
import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator


class DeviceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class_path: str = Field(alias="class")
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("class_path")
    @classmethod
    def class_reference(cls, value: str) -> str:
        module, separator, name = value.partition(":")
        if not separator or not name.isidentifier() or not all(part.isidentifier() for part in module.split(".")):
            raise ValueError("class must be module:Class (one class name, not an expression or factory)")
        return value

    @field_validator("kwargs")
    @classmethod
    def supplied_name(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if "name" in value:
            raise ValueError("kwargs.name is reserved; the service supplies the configured root identifier")
        return value

    def resolve_class(self) -> type:
        """Import an allowed driver class at startup, never while parsing TOML."""
        from ophyd import Device, Signal
        from ophyd_async.core import Device as AsyncDevice

        module, name = self.class_path.split(":")
        driver = getattr(importlib.import_module(module), name)
        if not isinstance(driver, type) or not issubclass(driver, (Device, Signal, AsyncDevice)):
            raise TypeError(f"{self.class_path} is not an ophyd Device/Signal or ophyd-async Device class")
        return driver


class EntraProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["entra"]
    tenant_id: UUID
    api_client_id: UUID


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: EntraProfileConfig
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("allowed_origins")
    @classmethod
    def serialized_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            port = parsed.port  # Access also rejects malformed/out-of-range ports.
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or "?" in origin
                or "#" in origin
                or "*" in origin
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in origin)
                or (port is None and parsed.netloc.endswith(":"))
            ):
                raise ValueError(
                    "allowed_origins must contain serialized browser origins without a trailing slash"
                )
            if parsed.scheme == "http" and parsed.hostname != "localhost":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    raise ValueError("HTTP origins are permitted only for localhost or loopback IP addresses")
        return origins


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth: AuthConfig
    devices: dict[str, DeviceSpec]
    connect_timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 10.0
    read_timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 5.0

    @field_validator("devices")
    @classmethod
    def root_identifiers(cls, value: dict[str, DeviceSpec]) -> dict[str, DeviceSpec]:
        for root in value:
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", root) is None:
                raise ValueError(f"invalid root identifier: {root!r}")
        return value


def load_config(path: Path) -> ServiceConfig:
    """Parse the complete file before any configured driver can be imported."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        return ServiceConfig.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
