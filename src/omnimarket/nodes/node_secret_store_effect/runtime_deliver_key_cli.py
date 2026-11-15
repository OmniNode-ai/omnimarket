# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point: deliver one declared key into its host env file (OMN-24611).

Usage:
    python -m omnimarket.nodes.node_secret_store_effect.runtime_deliver_key_cli \\
        --name <declared id> [--overlay-id <node overlay id>]

There is no ``--home`` flag: the home is the user's own, the handler's default,
because the file the agent's unit reads through ``EnvironmentFile=`` sits under
it. The value is never printed, logged or put on a command line: the typed
result model has no field that holds it, so the JSON on stdout carries the
outcome, the env name, the file's path and the length, never the value. A
refusal writes the typed JSON result on stderr, nothing on stdout, and exits 3.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnimarket.handlers.delegation_scenario_settings import load_store_overlay
from omnimarket.models.secret_store.model_deliver_key_request import (
    ModelDeliverKeyRequest,
)
from omnimarket.models.secret_store.model_deliver_key_result import (
    EnumDeliverKeyOutcome,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_deliver_key import (
    HandlerDeliverKeyToEnvFile,
)

_EXIT_NOT_OK = 3


def main(
    argv: list[str] | None = None, *, handler: HandlerDeliverKeyToEnvFile | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="deliver_key")
    parser.add_argument("--name", required=True)
    parser.add_argument("--overlay-id")
    args = parser.parse_args(argv)
    request = ModelDeliverKeyRequest(name=args.name)
    if handler is None:
        overlay = load_store_overlay(args.overlay_id) if args.overlay_id else None
        handler = (
            HandlerDeliverKeyToEnvFile(overlay=overlay)
            if overlay is not None
            else HandlerDeliverKeyToEnvFile()
        )
    result = asyncio.run(handler.handle(request))
    if (
        result.outcome is EnumDeliverKeyOutcome.DELIVERED
        or result.outcome is EnumDeliverKeyOutcome.UNCHANGED
    ):
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0
    sys.stderr.write(result.model_dump_json() + "\n")
    return _EXIT_NOT_OK


if __name__ == "__main__":
    sys.exit(main())
