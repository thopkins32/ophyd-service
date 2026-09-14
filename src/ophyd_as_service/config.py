"""Trusted, data-only device construction configuration."""

import importlib
import re
import tomllib
from pathlib import Path
from typing import Annotated

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


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
