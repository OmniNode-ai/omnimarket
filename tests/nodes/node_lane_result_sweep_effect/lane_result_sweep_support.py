# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the lane result sweep tests: an overlay, ledger lines and a clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
)
from omnimarket.nodes.node_error_class_normalize_compute.handlers.handler_error_class_normalize import (
    HandlerErrorClassNormalize,
)
from omnimarket.nodes.node_lane_result_sweep_effect.handlers.handler_lane_result_sweep import (
    HandlerLaneResultSweep,
)
from omnimarket.nodes.node_lane_result_sweep_effect.overlay import (
    ModelLaneResultSweepOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

SWEEP_AT = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
SINCE = SWEEP_AT - timedelta(hours=6)
BLOCKER = "start criterion false: piece 17b health line is unmerged"
OTHER_BLOCKER = "worktree guard refused the path"

FACTS: dict[str, Any] = {
    "configuration": {
        "strip_stages": [
            {
                "name": "tickets",
                "kind": "regex",
                "pattern": r"\b[A-Z]{2,8}-\d+\b",
                "replacement": "<ticket>",
            },
            {
                "name": "numbers",
                "kind": "regex",
                "pattern": r"\b\d+(?:\.\d+)?\b",
                "replacement": "<number>",
            },
        ],
        "producer_adapters": [
            {"source": "lane", "producer": "remote-lane-runner", "unique_codes": False}
        ],
        "lane_name_patterns": [],
        "max_open_triage": 4,
    },
    "hosts": [],
}


OUTCOME_CLASSES: dict[str, str] = {
    "done": "done",
    "complete": "done",
    "delivered": "done",
    "pass_evidence": "done",
    "handed-off": "handed-off",
    "skipped-owned": "skipped-owned",
    "skipped": "skipped-owned",
    "skipped-built": "skipped-owned",
    "skipped-already-built": "skipped-owned",
    "skipped-superseded": "skipped-owned",
    "superseded": "skipped-owned",
    "partial": "partial",
    "prepared-draft": "partial",
    "blocked": "blocked",
    "failed": "failed",
    "fail_contract_refused": "failed",
    "rejected-no-delegation": "failed",
    "timeout": "failed",
    "abandoned": "failed",
    "unknown": "unknown",
}
RESULT_CLASSES: dict[str, str] = {
    "blocked": "blocked",
    "external_blocker": "blocked",
    "cause_found_fix_needs_credential_owner": "blocked",
    "no_result_file": "failed",
    "push_failed": "failed",
    "push_timeout": "failed",
    "codex_capacity_retry": "failed",
    "fix_submitted": "handed-off",
    "cause_fix_submitted": "handed-off",
    "close_superseded": "handed-off",
    "armed": "handed-off",
    "waiting_ci": "handed-off",
    "cause_not_shared": "handed-off",
    "handed-off": "handed-off",
    "done": "done",
}


def class_key_of(text: str) -> str:
    """The key the sweep must compute for this error text, through the real normalizer."""
    result = HandlerErrorClassNormalize().handle(
        ModelErrorClassNormalizeRequest(
            event=ModelErrorEvent(
                source="lane", producer="remote-lane-runner", text=text
            ),
            facts=ModelErrorNormalizerFacts.model_validate(FACTS),
        )
    )
    return result.class_key


def overlay(**changes: Any) -> ModelLaneResultSweepOverlay:
    document: dict[str, Any] = {
        "ledger_read": {
            "route": "file",
            "path_env": "TEST_LEDGER_FILE",
            "timeout_seconds": 5,
            "row_limit": 1000,
        },
        "ledger_append": {
            "command_env": "TEST_APPEND_COMMAND",
            "host_env": "TEST_HOST",
            "timeout_seconds": 5,
        },
        "publish": {
            "lane": "lane-result-sweep",
            "actor": "node:node_lane_result_sweep_effect",
            "piece": 12,
            "action_deadline_seconds": 3600,
        },
        "source": "lane",
        "producer": "remote-lane-runner",
        "normalizer_facts": FACTS,
        "outcome_classes": OUTCOME_CLASSES,
        "result_classes": RESULT_CLASSES,
        "error_rule_document": {
            "error_rules": [
                {
                    "id": "start-criterion-false",
                    "class_keys": [class_key_of(BLOCKER)],
                    "mode": "shadow",
                    "action": {"kind": "fill-backlog-item"},
                }
            ]
        },
    }
    document.update(changes)
    return ModelLaneResultSweepOverlay.model_validate(document)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def terminal(
    lane: str,
    finished: datetime,
    outcome: str,
    *,
    blocker: str = "",
    result: str = "",
    engine_rc: int | None = None,
) -> str:
    cells = [f"lane={lane}", f"ticket=OMN-{abs(hash(lane)) % 90000 + 10000}"]
    cells.append(f"outcome={outcome}")
    if result:
        cells.append(f"lane_result={result}")
    if blocker:
        cells.append(f"blocker={blocker}")
    if engine_rc is not None:
        cells.append(f"engine_rc={engine_rc}")
    return f"{stamp(finished)} | TERMINAL | " + " | ".join(cells) + " | closing text"


def sweep_row(moment: datetime, action: str, receipt: str, *cells: str) -> str:
    return (
        f"{stamp(moment)} | STATUS | lane=lane-result-sweep | action={action} | "
        f"receipt={receipt} | " + " | ".join(cells)
    )


def park_row(moment: datetime, receipt: str, *, cite: str, review_by: datetime) -> str:
    cells = [f"review_by={stamp(review_by)}"]
    if cite:
        cells.insert(0, f"cite={cite}")
    return sweep_row(moment, "lane-result-park", receipt, *cells)


def receipt_key(lane: str, finished: datetime) -> str:
    return f"{lane}@{stamp(finished)}"


def handler(
    lines: list[str], *, now: datetime = SWEEP_AT, **changes: Any
) -> tuple[HandlerLaneResultSweep, InMemoryLedger]:
    ledger = InMemoryLedger(lines)
    sweep = HandlerLaneResultSweep(
        overlay=overlay(**changes),
        ledger=ledger,
        appender=ledger,
        clock=lambda: now,
    )
    return sweep, ledger
