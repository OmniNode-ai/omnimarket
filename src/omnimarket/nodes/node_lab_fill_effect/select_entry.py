# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shell entry for the lab-fill select step (OMN-19998).

Usage:
    python -m omnimarket.nodes.node_lab_fill_effect.select_entry < request.json > result.json

Exit 0: the selection ran and a result was written. Exit 2: the request (or its selection) does not
validate. Exit 3: a port failed. Exit 69: the ledger read was not ok (stale, unreadable or
not-moved); the typed result is still written to stdout so the driver reads the reason, and nothing
was selected. Exit 78: the reader overlay could not be loaded.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TextIO

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import build_point_read
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.runtime_work_ledger_reader import (
    build_sink,
)

from .handlers.handler_lab_fill_select import HandlerLabFillSelect
from .models.model_lab_fill_select import ModelLabFillSelectRequest
from .protocols.bus_lab_fill_ledger_rows import BusLabFillLedgerRows
from .protocols.protocol_lab_fill_effect import LabFillPortError

_EXIT_INVALID = 2
_EXIT_PORT = 3
_EXIT_LEDGER = 69
_EXIT_OVERLAY = 78
_STEP = "select_lab_fill_candidates"


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    selector: HandlerLabFillSelect | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    source = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        request = ModelLabFillSelectRequest.model_validate_json(source.read())
    except ValidationError as error:
        err.write(f"lab-fill select: invalid request: {error}\n")
        return _EXIT_INVALID
    if selector is None:
        env = os.environ if environ is None else environ
        try:
            overlay = load_overlay(env)
        except NodeOverlayError as error:
            err.write(
                f"lab-fill select: REFUSED: the reader overlay could not be loaded: {error}\n"
            )
            return _EXIT_OVERLAY
        reader = HandlerWorkLedgerReader(
            overlay,
            build_point_read(overlay.route, env),
            build_sink(overlay, env),
        )
        selector = HandlerLabFillSelect(BusLabFillLedgerRows(reader))
    try:
        result = selector.handle(request)
    except ValidationError as error:
        err.write(f"lab-fill select: invalid request: {error}\n")
        return _EXIT_INVALID
    except LabFillPortError as error:
        err.write(f"lab-fill select: step={_STEP} port failed: {error}\n")
        return _EXIT_PORT
    decisions = len(result.selection.decisions) if result.selection is not None else 0
    err.write(
        f"lab-fill select: step={_STEP} ledger={result.ledger_status} rows={result.ledger_rows}"
        f" decisions={decisions}\n"
    )
    out.write(result.model_dump_json() + "\n")
    return 0 if result.ledger_status == "ok" else _EXIT_LEDGER


if __name__ == "__main__":
    raise SystemExit(main())
