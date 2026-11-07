# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_commitment_check_runner_effect``: run every due commitment's check once.

What the schedule's unit runs. The ledger routes, the allowlist and every bound are the overlay's
(``ONEX_COMMITMENT_CHECK_OVERLAY`` pins the file, else ``node_commitment_check_runner_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``); the commitment row rules are node_commitment_ledger_compute's overlay.

Exit codes: 0 the run completed (a check that failed is still 0: it is a reading, not a fault of the run); 1 the run failed
(the ledger could not be read) or a reading was not accepted by the ledger; 3 a deployment fact is missing (an overlay or a
route variable).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    SubprocessCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckCommand,
    ModelCommitmentCheckRules,
)
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    ModelCommitmentRules,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)

NODE = "node_commitment_check_runner_effect"
LEDGER_NODE = "node_commitment_ledger_compute"
OVERLAY_ENV = "ONEX_COMMITMENT_CHECK_OVERLAY"
LEDGER_OVERLAY_ENV = "ONEX_COMMITMENT_OVERLAY"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m omnimarket.nodes.{NODE}")
    parser.add_argument(
        "--correlation-id", default=None, help="defaults to a fresh id for this run"
    )
    arguments = parser.parse_args(argv)
    try:
        rules = load_structured_overlay(
            NODE, ModelCommitmentCheckRules, file_env=OVERLAY_ENV
        )
        commitment_rules = load_structured_overlay(
            LEDGER_NODE, ModelCommitmentRules, file_env=LEDGER_OVERLAY_ENV
        )
        source = build_row_source(rules.ledger_read)
        appender = build_appender(rules.ledger_append, rules.runner_lane)
    except (NodeOverlayError, LedgerRouteError) as exc:
        sys.stderr.write(f"commitment-check: refused ({type(exc).__name__}: {exc})\n")
        return 3
    handler = HandlerCommitmentCheckRunner(
        rules=rules,
        commitment_rules=commitment_rules,
        source=source,
        appender=appender,
        executor=SubprocessCheckExecutor(rules.output_tail_chars),
        clock=lambda: datetime.now(UTC),
    )
    result = handler.handle(
        ModelCommitmentCheckCommand(
            correlation_id=arguments.correlation_id or uuid.uuid4().hex
        )
    )
    if result.status == "failed":
        sys.stdout.write(f"commitment-check: FAILED {result.failure}\n")
        return 1
    lines = [
        f"{o.commitment_id} {o.status_before.value}->{o.status_after.value} "
        f"{o.result} exit={o.exit_code}"
        for o in result.outcomes
    ]
    lines.append(
        f"commitment-check: checked={sum(1 for o in result.outcomes if o.ran)} "
        f"refused={sum(1 for o in result.outcomes if not o.ran)} malformed={len(result.malformed)}"
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return (
        1 if any(o.result.startswith("append-failed:") for o in result.outcomes) else 0
    )


__all__ = ["main"]
