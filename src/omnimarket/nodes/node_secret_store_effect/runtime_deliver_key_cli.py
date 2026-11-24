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

Exit 3 also covers what stops the handler being built or asked at all: a name
outside the declared-id pattern, a malformed node contract, and an
``--overlay-id`` that names no overlay (``load_store_overlay`` answers ``None``
when no overlay file is found under the overlay roots) or a broken one (it
raises). A named overlay that cannot be loaded never falls back to the host env
file store: the operator asked for one store and gets a refusal, not another.
An error raised out of the handler itself is a refusal too, named by its type.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import yaml
from pydantic import ValidationError

from omnimarket.handlers.delegation_scenario_settings import load_store_overlay
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.secret_store.model_deliver_key_request import (
    ModelDeliverKeyRequest,
)
from omnimarket.models.secret_store.model_deliver_key_result import (
    EnumDeliverKeyOutcome,
    ModelDeliverKeyResult,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_deliver_key import (
    HandlerDeliverKeyToEnvFile,
)

_EXIT_NOT_OK = 3


def _refusal(name: str, outcome: EnumDeliverKeyOutcome, detail: str) -> int:
    """The typed refusal of a delivery that never reached the handler: JSON on stderr, exit 3."""
    result = ModelDeliverKeyResult(name=name, outcome=outcome, detail=detail)
    sys.stderr.write(result.model_dump_json() + "\n")
    return _EXIT_NOT_OK


def main(
    argv: list[str] | None = None, *, handler: HandlerDeliverKeyToEnvFile | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="deliver_key")
    parser.add_argument("--name", required=True)
    parser.add_argument("--overlay-id")
    args = parser.parse_args(argv)
    try:
        request = ModelDeliverKeyRequest(name=args.name)
    except ValidationError:
        return _refusal(
            "invalid_name",
            EnumDeliverKeyOutcome.NOT_DECLARED,
            "the name is not a declared id shape; nothing was read and nothing was written",
        )
    if handler is None:
        overlay = None
        if args.overlay_id is not None:
            if not args.overlay_id.strip():
                return _refusal(
                    args.name,
                    EnumDeliverKeyOutcome.KEY_UNREADABLE,
                    "--overlay-id is empty (an unset shell variable?); the host env "
                    "file store is not used in its place; nothing was read and "
                    "nothing was written",
                )
            try:
                overlay = load_store_overlay(args.overlay_id)
            except NodeOverlayError as exc:
                return _refusal(
                    args.name,
                    EnumDeliverKeyOutcome.KEY_UNREADABLE,
                    f"the overlay {args.overlay_id!r} could not be loaded "
                    f"({type(exc).__name__}); nothing was read and nothing was written",
                )
            if overlay is None:
                return _refusal(
                    args.name,
                    EnumDeliverKeyOutcome.KEY_UNREADABLE,
                    f"the overlay {args.overlay_id!r} names no overlay file under the "
                    "overlay roots; the host env file store is not used in its place; "
                    "nothing was read and nothing was written",
                )
        try:
            handler = (
                HandlerDeliverKeyToEnvFile(overlay=overlay)
                if overlay is not None
                else HandlerDeliverKeyToEnvFile()
            )
        except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
            return _refusal(
                args.name,
                EnumDeliverKeyOutcome.NOT_DECLARED,
                f"the node contract could not be loaded ({type(exc).__name__}); "
                "nothing was read and nothing was written",
            )
    try:
        result = asyncio.run(handler.handle(request))
    except Exception as exc:
        # The handler answers with typed outcomes; an error raised past it is still a
        # refusal here, named by its type only (a message could carry a value).
        return _refusal(
            args.name,
            EnumDeliverKeyOutcome.KEY_UNREADABLE,
            f"the delivery stopped on an unexpected {type(exc).__name__}; "
            "the state of the env file is not known",
        )
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
