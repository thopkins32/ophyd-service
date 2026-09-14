"""The supported single-process, loopback-default service entry point."""

import argparse
from pathlib import Path

import uvicorn

from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve configured Ophyd devices read-only")
    parser.add_argument("--config", required=True, type=Path, help="Trusted local TOML device configuration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))

    # Even built-in driver packages must not load before the whole config is valid.
    from .api import create_app
    from .subscriptions import WS_MAX_MESSAGE_BYTES

    uvicorn.run(
        create_app(config),
        host=args.host,
        port=args.port,
        workers=1,
        reload=False,
        loop="asyncio",
        ws="websockets",
        ws_max_size=WS_MAX_MESSAGE_BYTES,
    )
