# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commitments source is one per-source reader under its own overlay budget (OMN-23013 AC2, on OMN-23014's shape).

A commitment is read like every other source: fresh within its budget, stale from its last snapshot when the read
stalls, unread with its cause when its overlay or the ledger cannot be read. The failing controls are the same reads
with the overlay absent and with no snapshot to serve, which must read differently.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkItemKind,
    EnumWorkStateSource,
    ModelSourceBudget,
    ModelWorkStateRules,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.work_state_source_budget import (
    InMemorySnapshotStore,
    SourceRead,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    build_request,
    source_readers,
)
from tests.nodes.node_commitment_check_runner_effect.support import (
    open_commitment_line,
)

pytestmark = pytest.mark.unit

COMMITMENTS = EnumWorkStateSource.COMMITMENTS
OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "OMNI_HOME": str(tmp_path),
        "ONEX_WORK_STATE_PLANS_ROOT": str(tmp_path),
        "ONEX_COMMITMENT_OVERLAY": str(OVERLAY),
    }


@pytest.fixture
def release() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()


def _budgeted(rules: ModelWorkStateRules, seconds: float) -> ModelWorkStateRules:
    """The rules with the commitments source on a test-sized budget."""
    budgets = dict(rules.source_budgets)
    budgets[COMMITMENTS] = ModelSourceBudget(
        timeout_seconds=seconds,
        budget_seconds=seconds,
        attempts=1,
        max_stale_age_seconds=3600,
    )
    return rules.model_copy(update={"source_budgets": budgets})


def _status(request, source: EnumWorkStateSource):
    [row] = [s for s in request.source_status if s.source is source]
    return row


def test_the_commitments_source_is_one_reader_among_the_sources(
    rules, now, tmp_path, env
) -> None:
    specs = source_readers(now, tmp_path, env, InMemoryLedger([]), rules)
    assert COMMITMENTS in [spec.source for spec in specs]
    assert [spec.source for spec in specs].count(COMMITMENTS) == 1
    assert set(rules.source_budgets) == set(EnumWorkStateSource)


def test_an_open_commitment_is_read_fresh_and_projected_as_work(
    rules, now, tmp_path, env
) -> None:
    ledger = InMemoryLedger([open_commitment_line(minutes_ago=9 * 24 * 60)])
    request = build_request(now, tmp_path, env, ledger, rules)
    assert _status(request, COMMITMENTS).reading is EnumSourceReading.FRESH
    assert len(request.commitments) == 1
    result = HandlerWorkStateProjection().handle(request)
    kinds = [i.kind for i in result.items]
    assert kinds.count(EnumWorkItemKind.COMMITMENT) == 1


def test_a_stalled_commitments_read_serves_the_snapshot_stale(
    rules, now, tmp_path, env, release: threading.Event
) -> None:
    store = InMemorySnapshotStore()
    ledger = InMemoryLedger([open_commitment_line(minutes_ago=9 * 24 * 60)])
    first = build_request(
        now - timedelta(seconds=120), tmp_path, env, ledger, rules, store=store
    )
    assert len(first.commitments) == 1

    def stall() -> SourceRead:
        release.wait()
        return SourceRead()

    second = build_request(
        now,
        tmp_path,
        env,
        ledger,
        _budgeted(rules, 0.1),
        store=store,
        reader_overrides={COMMITMENTS: stall},
    )
    status = _status(second, COMMITMENTS)
    assert status.reading is EnumSourceReading.STALE
    assert status.age_seconds == 120
    assert [c.commitment_id for c in second.commitments] == [
        c.commitment_id for c in first.commitments
    ]


def test_control_a_stalled_commitments_read_with_no_snapshot_is_unread(
    rules, now, tmp_path, env, release: threading.Event
) -> None:
    """Failing control: with no snapshot to serve the stalled source reads unread, so the stale reading above can fail."""

    def stall() -> SourceRead:
        release.wait()
        return SourceRead()

    request = build_request(
        now,
        tmp_path,
        env,
        InMemoryLedger([]),
        _budgeted(rules, 0.1),
        store=InMemorySnapshotStore(),
        reader_overrides={COMMITMENTS: stall},
    )
    assert _status(request, COMMITMENTS).reading is EnumSourceReading.UNREAD
    assert request.commitments == ()
    assert any(u.startswith("commitments:") for u in request.unread)


def test_an_absent_commitment_overlay_is_unread_with_its_cause(
    rules, now, tmp_path, env
) -> None:
    env = {k: v for k, v in env.items() if k != "ONEX_COMMITMENT_OVERLAY"}
    request = build_request(now, tmp_path, env, InMemoryLedger([]), rules)
    status = _status(request, COMMITMENTS)
    assert status.reading is EnumSourceReading.UNREAD
    assert "overlay" in status.cause
    assert any(u.startswith("commitments:overlay") for u in request.unread)
    # the other sources are not held back by it: the ledger source still reads
    assert (
        _status(request, EnumWorkStateSource.LEDGER).reading is EnumSourceReading.FRESH
    )


def test_a_commitment_ledger_that_cannot_be_read_names_the_source(
    rules, now, tmp_path, env
) -> None:
    ledger = InMemoryLedger([])
    ledger.read_error = "route-down"
    request = build_request(now, tmp_path, env, ledger, rules)
    assert _status(request, COMMITMENTS).reading is EnumSourceReading.UNREAD
    assert any(
        u.startswith("commitments:") and "route-down" in u for u in request.unread
    )
