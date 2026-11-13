# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the commitment check runner on the runtime schedule (OMN-24029)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.commitment_check_scheduled_tick import (
    LedgerRouteError,
    ModelCommitmentCheckTickOutcome,
    run_commitment_check_tick,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    InMemoryCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_scheduled_effect.handlers.handler_commitment_check_scheduled import (
    HandlerCommitmentCheckScheduled,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_commitment_check_scheduled_effect"
RUNNER_NODE = "node_commitment_check_runner_effect"
RUNNER_EXAMPLE = REPO / f"src/omnimarket/nodes/{RUNNER_NODE}/overlay.example.yaml"
LEDGER_EXAMPLE = (
    REPO / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
)
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelCommitmentCheckTickOutcome(
    False, "read=1 live=1 checked=1 refused=0 malformed=0 deferred=0"
)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "commitment_check",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "check_overlay_id": RUNNER_NODE,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def runner_environ(tmp_path: Path) -> dict[str, str]:
    """The runner's and the ledger's own overlays, from the checked-in examples."""
    for node, example in (
        (RUNNER_NODE, RUNNER_EXAMPLE),
        ("node_commitment_ledger_compute", LEDGER_EXAMPLE),
    ):
        (tmp_path / node).mkdir()
        (tmp_path / node / "overlay.yaml").write_text(
            example.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingTick:
    """The injected tick: returns the fixed outcome and records the overlay id it was given."""

    def __init__(self, outcome: ModelCommitmentCheckTickOutcome) -> None:
        self._outcome = outcome
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        check_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelCommitmentCheckTickOutcome:
        self.calls.append(check_overlay_id)
        return self._outcome


def _raising(error: Exception) -> Any:
    def tick(
        *,
        check_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelCommitmentCheckTickOutcome:
        raise error

    return tick


def test_golden_a_due_tick_runs_the_runner_and_returns_its_detail(
    tmp_path: Path,
) -> None:
    recorded = _RecordingTick(DONE)
    h = HandlerCommitmentCheckScheduled(environ=environ(tmp_path), tick=recorded)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "commitment_check",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail
    assert recorded.calls == [RUNNER_NODE]


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerCommitmentCheckScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None


def test_error_an_unreadable_check_overlay_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerCommitmentCheckScheduled(
        environ=environ(tmp_path),
        tick=_raising(NodeOverlayError("boom: the overlay is invalid")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the commitment check overlay could not be read",
    )


def test_error_an_unbound_ledger_source_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerCommitmentCheckScheduled(
        environ=environ(tmp_path),
        tick=_raising(LedgerRouteError("ONEX_COMMITMENT_LEDGER_SOURCE is not set")),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the commitment ledger source is not bound in the runtime",
    )


def test_error_a_failed_run_outcome_carries_its_detail(tmp_path: Path) -> None:
    failed = ModelCommitmentCheckTickOutcome(
        True, "the commitment check run failed: ledger-unreadable:down"
    )
    h = HandlerCommitmentCheckScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(failed)
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == failed.detail


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerCommitmentCheckScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerCommitmentCheckScheduled(
        environ=environ(tmp_path), tick=_RecordingTick(DONE)
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("commitment_check")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


def test_real_chain_an_empty_ledger_source_reads_failed_with_no_live_commitment(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([])
    outcome = run_commitment_check_tick(
        check_overlay_id=RUNNER_NODE,
        environ=runner_environ(tmp_path),
        clock=lambda: T0,
        source=ledger,
        appender=ledger,
        executor=InMemoryCheckExecutor({}),
    )
    assert outcome.failed is True
    assert outcome.detail == "no live commitment on the ledger; nothing was checked"
    assert ledger.lines == []
