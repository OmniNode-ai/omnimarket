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

``--exec-names "A B" -- <command...>`` is the launcher contract (OMN-22792): each
named key the environment does not already set is resolved into this process's
environment, then the command replaces this process with that environment. A
key that does not resolve is named on stderr with its typed refusal and left
unset, and the command still starts; no value is ever written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import NoReturn

from omnimarket.models.secret_store.model_house_model_key_request import (
    ModelHouseModelKeyRequest,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HandlerHouseModelKey,
)

_EXIT_NOT_OK = 3


def _exec_with_keys(names: list[str], command: list[str]) -> NoReturn:
    """Resolve each unset name into the environment, then replace this process with ``command``."""
    for name in names:
        if os.environ.get(name):
            continue
        result = asyncio.run(
            HandlerHouseModelKey().handle(ModelHouseModelKeyRequest(name=name))
        )
        if result.ok and result.value is not None:
            os.environ[name] = result.value.get_secret_value()
        else:
            sys.stderr.write(result.model_dump_json() + "\n")
            sys.stderr.write(f"house key {name} did not resolve; starting without it\n")
    sys.stderr.flush()
    os.execvp(command[0], command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="house_model_key")
    parser.add_argument("--name")
    parser.add_argument("--value-only", action="store_true")
    parser.add_argument("--exec-names")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.exec_names is not None:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("--exec-names needs a command after --")
        names = args.exec_names.split()
        for name in names:
            ModelHouseModelKeyRequest(
                name=name
            )  # a malformed name refuses before any read
        _exec_with_keys(names, command)
    if args.name is None or args.command:
        parser.error(
            "--name is required, and a command is taken only with --exec-names"
        )
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
