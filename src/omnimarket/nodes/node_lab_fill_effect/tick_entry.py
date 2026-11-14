# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shell entry for the whole lab-fill tick with no model in the loop (OMN-19998).

Usage:
    python -m omnimarket.nodes.node_lab_fill_effect.tick_entry < request.json > result.json

Reads one ModelLabFillTickRequest as JSON on stdin, runs the run_lab_fill_tick operation and writes
the ModelLabFillTickResult as JSON on stdout. Exit 0: a result was written (a tick that placed
nothing is a result, read from ``outcome``). Exit 2: the request does not validate. Exit 3: a port
could not read or act.
"""

from __future__ import annotations

import sys
from typing import TextIO

from pydantic import ValidationError

from .handlers.handler_lab_fill_tick import HandlerLabFillTick
from .models.model_lab_fill_tick import ModelLabFillTickRequest
from .protocols.protocol_lab_fill_effect import LabFillPortError

_EXIT_INVALID = 2
_EXIT_PORT = 3
_STEP = "run_lab_fill_tick"


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    ticker: HandlerLabFillTick | None = None,
) -> int:
    source = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        request = ModelLabFillTickRequest.model_validate_json(source.read())
    except ValidationError as error:
        err.write(f"lab-fill tick: invalid request: {error}\n")
        return _EXIT_INVALID
    try:
        result = (ticker if ticker is not None else HandlerLabFillTick()).handle(
            request
        )
    except LabFillPortError as error:
        err.write(f"lab-fill tick: step={_STEP} port failed: {error}\n")
        return _EXIT_PORT
    err.write(
        f"lab-fill tick: step={_STEP} outcome={result.outcome} free_slots={result.free_slots}"
        f" placed={result.placed} refusals={result.refusals_cell}\n"
    )
    out.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
