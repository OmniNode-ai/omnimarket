# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_completion_rate_effect <verb>``.

``read``   read the window, run the completion-rate compute node and append the row (what a schedule runs).
``probe``  the same read, appending nothing.

Exit codes: 0 the read ran and its row was written (NOT WORKING and UNKNOWN are still 0: a reading changes nothing and refuses
nothing); 1 the run failed (a row that did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationRateOverlay,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    SshPostgresEventSource,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.handlers.handler_delegation_completion_rate_run import (
    HandlerDelegationCompletionRateRun,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_delegation_rate_run import (
    ModelDelegationRateCommand,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    NODE_NAME,
    ModelDelegationRateEffectOverlay,
    load_overlay,
    load_rate_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def build_handler(
    overlay: ModelDelegationRateEffectOverlay,
    rate_overlay: ModelDelegationRateOverlay,
    environ: Mapping[str, str],
    write: bool,
) -> HandlerDelegationCompletionRateRun:
    return HandlerDelegationCompletionRateRun(
        rate_overlay=rate_overlay,
        source=SshPostgresEventSource(overlay.events_source, environ),
        appender=build_appender(overlay.ledger_append, rate_overlay.row_lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )


def _run(write: bool) -> int:
    overlay = load_overlay()
    rate_overlay = load_rate_overlay()
    environ = os.environ  # ONEX_FLAG_EXEMPT: variable names are declared by the overlay
    handler = build_handler(overlay, rate_overlay, environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelDelegationRateCommand(correlation_id=f"delegation-rate-{now}", write=write)
    )
    if result.status == "failed":
        sys.stderr.write(f"DELEGATION-RATE-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING verdict={result.verdict.value} r_all={result.r_all} r_dep={result.r_dep} "
        f"failed_rate={result.failed_rate} rows={result.rows_in_window} ledger={result.ledger} "
        f"reason={','.join(result.reasons) or 'none'}\n"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("read", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "read")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
