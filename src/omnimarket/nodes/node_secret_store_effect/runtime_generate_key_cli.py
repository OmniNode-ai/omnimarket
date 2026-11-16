# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point: generate one declared key through the node (OMN-23695).

Usage:
    python -m omnimarket.nodes.node_secret_store_effect.runtime_generate_key_cli \\
        --name <declared id> [--overlay-id <node overlay id>]

The value is never printed, logged or put on a command line: the typed result
model has no field that holds it, so the JSON on stdout carries the outcome,
the reference, the key name and the length, never the value. A refusal writes
the typed JSON result on stderr, nothing on stdout, and exits 3.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnimarket.handlers.delegation_scenario_settings import load_store_overlay
from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_generate_key import (
    HandlerGenerateKey,
)

_EXIT_NOT_OK = 3


def main(
    argv: list[str] | None = None, *, handler: HandlerGenerateKey | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="generate_key")
    parser.add_argument("--name", required=True)
    parser.add_argument("--overlay-id")
    args = parser.parse_args(argv)
    request = ModelGenerateKeyRequest(name=args.name)
    if handler is None:
        overlay = load_store_overlay(args.overlay_id) if args.overlay_id else None
        handler = (
            HandlerGenerateKey(overlay=overlay)
            if overlay is not None
            else HandlerGenerateKey()
        )
    result = asyncio.run(handler.handle(request))
    if (
        result.outcome is EnumSecretStoreOutcome.CREATED
        or result.outcome is EnumSecretStoreOutcome.EXISTS
    ):
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0
    sys.stderr.write(result.model_dump_json() + "\n")
    return _EXIT_NOT_OK


if __name__ == "__main__":
    sys.exit(main())
