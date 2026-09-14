"""Snapshot driver-owned values once before encoding or deferred delivery."""

from collections.abc import Mapping
from enum import Enum
from typing import Any

import numpy as np
import orjson
from pydantic import BaseModel


def snapshot_json(value: Any) -> Any:
    """Own mutable values without flattening arrays or losing integer precision.

    Numeric arrays get one C-contiguous, native-byte-order copy. Encoding may
    reuse that snapshot for every client; callers must not normalize it again.
    Unsupported objects are errors, never a string representation of a reading.
    """
    if isinstance(value, Enum):
        return snapshot_json(value.value)
    if isinstance(value, BaseModel):
        return snapshot_json(value.model_dump(mode="python"))
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "biuf":
            return np.array(value, dtype=value.dtype.newbyteorder("="), order="C", copy=True)
        if value.dtype.kind == "U":
            return value.tolist()
        if value.dtype.kind == "S":
            return value.astype(str).tolist()
        raise TypeError(f"Unsupported ndarray dtype: {value.dtype}")
    if isinstance(value, np.generic):
        scalar = value.item()
        if isinstance(scalar, np.generic):
            raise TypeError(f"Unsupported NumPy scalar dtype: {value.dtype}")
        return snapshot_json(scalar)
    if isinstance(value, Mapping):
        return {key: snapshot_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def encode_json(value: Any) -> bytes:
    """Encode an owned snapshot; JSON non-finite floats are consistently null."""
    return orjson.dumps(value, option=orjson.OPT_SERIALIZE_NUMPY)
