# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point: resolve one house model key from the secret store (OMN-22417).

Usage:
    python -m omnimarket.nodes.node_secret_store_effect.runtime_house_model_key_cli \\
        --name LLM_GLM_API_KEY [--value-only]

Default: one JSON result on stdout carrying the outcome, the secret reference
and the value's length, never the value. ``--value-only`` is the pipe contract
of an in-host consumer (the GLM harness login): the value alone on stdout, read
by the parent process into memory. A refusal prints the typed JSON result on
stderr and exits 3, with nothing on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from omnimarket.models.secret_store.model_house_model_key_request import (
    ModelHouseModelKeyRequest,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HandlerHouseModelKey,
)

_EXIT_NOT_OK = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="house_model_key")
    parser.add_argument("--name", required=True)
    parser.add_argument("--value-only", action="store_true")
    args = parser.parse_args(argv)
    request = ModelHouseModelKeyRequest(name=args.name)
    result = asyncio.run(HandlerHouseModelKey().handle(request))
    if not result.ok:
        sys.stderr.write(result.model_dump_json() + "\n")
        return _EXIT_NOT_OK
    if args.value_only and result.value is not None:
        sys.stdout.write(result.value.get_secret_value())
    else:
        sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
