# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the lab-fill send step (OMN-23302).

Usage:
    python -m omnimarket.nodes.node_lab_fill_effect < request.json > result.json

Reads one ModelLabFillSendRequest as JSON on stdin, runs the send_lab_fill_lane operation (render the brief, send gate,
brief check, launch) and writes the ModelLabFillLaunchResult as JSON on stdout, so a fill driver that runs shell
commands (the launchd lab-fill job) sends each planned lane through the node. One line on stderr names the step and
the lane's outcome, so the driver's tick log shows the node's send step ran.

Exit 0 when the step ran and a result was written (a lane the send gate or the brief check stopped is a result, read
from ``skipped``), 2 when the request does not validate, 3 when a port could not read or act (the reason is on stderr).
"""

from __future__ import annotations

import sys
from typing import TextIO

from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_send import (
    HandlerLabFillSend,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_send import (
    ModelLabFillSendRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.protocol_lab_fill_effect import (
    LabFillPortError,
)

_EXIT_INVALID = 2
_EXIT_PORT = 3
_STEP = "send_lab_fill_lane"


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sender: HandlerLabFillSend | None = None,
) -> int:
    source = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        request = ModelLabFillSendRequest.model_validate_json(source.read())
    except ValidationError as error:
        err.write(f"lab-fill send: invalid request: {error}\n")
        return _EXIT_INVALID
    try:
        result = (sender if sender is not None else HandlerLabFillSend()).handle(
            request
        )
    except LabFillPortError as error:
        err.write(f"lab-fill send: {_STEP} port failed: {error}\n")
        return _EXIT_PORT
    err.write(
        f"lab-fill send: step={_STEP} lane={result.lane} detached={str(result.detached).lower()}"
        f" skipped={result.skipped or '-'}\n"
    )
    out.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
