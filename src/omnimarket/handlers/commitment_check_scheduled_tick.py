# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled job: read the runner's overlays, build the runner's ports, run one run.

The scheduled job node reaches the commitment check runner through this shared module, so no node imports another
node's models package. The runner rules are the runner's own overlay (``ONEX_COMMITMENT_CHECK_OVERLAY`` pins the file,
else ``<check_overlay_id>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``); the commitment row rules are
node_commitment_ledger_compute's overlay (``ONEX_COMMITMENT_OVERLAY`` pins the file). An unreadable overlay raises
``NodeOverlayError`` and an unbindable ledger route raises ``LedgerRouteError``; the scheduled job node turns both
into failed run events.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.commitment import ModelCommitmentRules
from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    SubprocessCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.schedule import (
    load_schedule,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckCommand,
    ModelCommitmentCheckRules,
)
from omnimarket.nodes.node_commitment_check_runner_effect.protocols import (
    ProtocolCheckExecutor,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)


class ModelCommitmentCheckTickOutcome(NamedTuple):
    """How one scheduled run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def run_commitment_check_tick(
    *,
    check_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    source: ProtocolLedgerRowSource | None = None,
    appender: ProtocolReadingAppender | None = None,
    executor: ProtocolCheckExecutor | None = None,
) -> ModelCommitmentCheckTickOutcome:
    """Run every due commitment's check once, the way the runner's unit does."""
    rules = load_structured_overlay(
        check_overlay_id,
        ModelCommitmentCheckRules,
        environ,
        file_env="ONEX_COMMITMENT_CHECK_OVERLAY",
    )
    commitment_rules = load_structured_overlay(
        "node_commitment_ledger_compute",
        ModelCommitmentRules,
        environ,
        file_env="ONEX_COMMITMENT_OVERLAY",
    )
    handler = HandlerCommitmentCheckRunner(
        rules=rules,
        commitment_rules=commitment_rules,
        source=(
            source
            if source is not None
            else build_row_source(rules.ledger_read, environ)
        ),
        appender=(
            appender
            if appender is not None
            else build_appender(rules.ledger_append, rules.runner_lane, environ)
        ),
        executor=(
            executor
            if executor is not None
            else SubprocessCheckExecutor(rules.output_tail_chars)
        ),
        clock=clock,
        run_budget_seconds=load_schedule().timeout_seconds,
    )
    result = handler.handle(
        ModelCommitmentCheckCommand(correlation_id=uuid.uuid4().hex)
    )
    if result.status == "failed":
        return ModelCommitmentCheckTickOutcome(
            True, f"the commitment check run failed: {result.failure}"
        )
    if result.live == 0:
        return ModelCommitmentCheckTickOutcome(
            True, "no live commitment on the ledger; nothing was checked"
        )
    if any(o.result.startswith("append-failed:") for o in result.outcomes):
        return ModelCommitmentCheckTickOutcome(
            True, "a reading was not accepted by the ledger"
        )
    detail = (
        f"read={result.read} live={result.live} "
        f"checked={sum(1 for o in result.outcomes if o.ran)} "
        f"refused={sum(1 for o in result.outcomes if not o.ran)} "
        f"malformed={len(result.malformed)} deferred={result.deferred}"
    )
    return ModelCommitmentCheckTickOutcome(False, detail)


__all__: list[str] = [
    "LedgerRouteError",
    "ModelCommitmentCheckTickOutcome",
    "run_commitment_check_tick",
]
