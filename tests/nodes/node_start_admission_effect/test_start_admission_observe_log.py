# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-admission effect keeps an observe log: one ledger row per decision (OMN-23306)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
)
from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionLogRow,
    ModelStartAdmissionPolicy,
    ModelStartAdmissionRequest,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_ledger import (
    LedgerStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
STORE_POLICY = ModelGateStorePolicy(
    backend=EnumGateStoreBackend.IN_MEMORY,
    limits={"alpha": 1, "beta": 1},
    reservation_ttl_seconds=60,
    order=(),
)


def _handler(
    ledger: InMemoryLedger, mode: EnumAdmissionMode
) -> tuple[HandlerStartAdmission, LedgerStartAdmissionLog]:
    store = InMemoryGateStore(STORE_POLICY)
    store.transact(
        ModelGateStoreRequest(
            correlation_id="setup",
            operation=EnumGateStoreOperation.RECORD_SAFETY,
            step_key="setup/stop",
            piece="alpha",
            safety_state="stop",
        )
    )
    log = LedgerStartAdmissionLog(
        ledger, lane="observe-lane", actor="node:test", host="h1", clock=lambda: NOW
    )
    policy = ModelStartAdmissionPolicy(
        default_mode=mode, modes={}, answer_deadline_seconds=5.0, max_redecisions=1
    )
    return HandlerStartAdmission(store, log, policy), log


def _request(
    piece: str, key: str, *, drill: bool = False
) -> ModelStartAdmissionRequest:
    return ModelStartAdmissionRequest(
        correlation_id="c1",
        step_key=key,
        path="lane_runner",
        piece=piece,
        reservation_id=key,
        drill=drill,
    )


def test_observe_log_one_ledger_row_per_decision_with_the_refusal_it_would_make() -> (
    None
):
    ledger = InMemoryLedger()
    handler, log = _handler(ledger, EnumAdmissionMode.OBSERVE)
    refused = handler.handle(_request("alpha", "run1/a"))
    admitted = handler.handle(_request("beta", "run1/b"))
    assert refused.outcome is EnumAdmissionOutcome.OBSERVED_REFUSAL
    assert refused.proceed
    assert admitted.outcome is EnumAdmissionOutcome.ADMITTED
    assert len(ledger.lines) == 2 == log.written
    first, second = ledger.lines
    for cell in (
        "| STATUS |",
        "lane=observe-lane",
        "src=start-admission-observe",
        "drill=0",
        "path=lane_runner",
        "piece=alpha",
        "mode=observe",
        "outcome=observed_refusal",
        "proceed=1",
        "reason=refused-stop",
    ):
        assert cell in first, cell
    assert "outcome=admitted" in second
    assert "reason=none" in second


def test_observe_log_a_drill_request_is_tagged_and_an_enforced_refusal_is_logged() -> (
    None
):
    ledger = InMemoryLedger()
    handler, _ = _handler(ledger, EnumAdmissionMode.ENFORCE)
    result = handler.handle(_request("alpha", "run2/a", drill=True))
    assert result.drill
    assert not result.proceed
    assert "drill=1" in ledger.lines[0]
    assert "outcome=refused" in ledger.lines[0]
    assert "proceed=0" in ledger.lines[0]


def test_observe_log_a_row_the_ledger_did_not_take_is_kept_not_dropped() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    handler, log = _handler(ledger, EnumAdmissionMode.OBSERVE)
    result = handler.handle(_request("alpha", "run3/a"))
    assert result.proceed
    assert ledger.lines == []
    assert log.written == 0
    assert log.failed == ["run3/a:refused"]


def test_observe_log_a_replayed_decision_is_one_row() -> None:
    ledger = InMemoryLedger()
    handler, log = _handler(ledger, EnumAdmissionMode.OBSERVE)
    handler.handle(_request("beta", "run4/a"))
    log.append(
        ModelStartAdmissionLogRow(
            correlation_id="c1",
            step_key="run4/a",
            path="lane_runner",
            piece="beta",
            mode=EnumAdmissionMode.OBSERVE,
            outcome=EnumAdmissionOutcome.ADMITTED,
            proceed=True,
        )
    )
    assert len(ledger.lines) == 1
