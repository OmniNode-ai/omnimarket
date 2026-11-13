# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_hook_tool_outcome_compute``.

Reads JSON Lines from stdin. Each line is an object with an ``id`` and a
``request``. Writes one JSON line to stdout per input line.

Exit codes: 0 every line was answered; 3 a line or the overlay is bad
(INDETERMINATE).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_hook_tool_outcome_compute.handlers.handler_hook_tool_outcome import (
    HandlerHookToolOutcome,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelHookToolOutcomeRequest,
    ModelHookToolOutcomeResult,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.overlay import (
    NODE_NAME,
    load_outcome_rules,
)


def _read_lines(stdin: Any) -> list[str]:
    return [line for line in stdin.read().splitlines() if line.strip()]


def _validate_line(line: str, index: int) -> tuple[str, ModelHookToolOutcomeRequest]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"line {index}: not a JSON object: {exc}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"line {index}: not a JSON object")
    ident = obj.get("id")
    if not isinstance(ident, str) or not ident:
        raise ValueError(f"line {index}: missing non-empty string 'id'")
    request_raw = obj.get("request")
    if not isinstance(request_raw, dict):
        raise ValueError(f"line {index}: missing 'request' object")
    try:
        request = ModelHookToolOutcomeRequest.model_validate(request_raw)
    except ValidationError as exc:
        raise ValueError(f"line {index}: request validation failed: {exc}") from None
    return ident, request


def _run(
    argv: Sequence[str] | None,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    environ: Mapping[str, str] | None,
) -> int:
    try:
        rules = load_outcome_rules(environ)
    except (NodeOverlayError, ValidationError) as exc:
        stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3

    handler = HandlerHookToolOutcome(rules)
    lines = _read_lines(stdin)

    parsed: list[tuple[str, ModelHookToolOutcomeRequest]] = []
    for index, line in enumerate(lines, start=1):
        try:
            parsed.append(_validate_line(line, index))
        except ValueError as exc:
            stderr.write(f"INDETERMINATE: ValueError: {exc}\n")
            return 3

    for ident, request in parsed:
        result: ModelHookToolOutcomeResult = handler.handle(request)
        stdout.write(
            json.dumps(
                {"id": ident, "result": result.model_dump(mode="json")},
                separators=(",", ":"),
            )
            + "\n"
        )
    return 0


def main(
    argv: Sequence[str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr
    return _run(argv, stdin, stdout, stderr, environ)


if __name__ == "__main__":
    raise SystemExit(main())
