# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-25856: a dod-verify-start command is bounded as a whole.

Measured on the dev runtime (2026-11-25): a start command for a contract of
about 100 criteria across three repositories ran its checks serially, each up
to 30 s (``DOD_VERIFY_CHECK_TIMEOUT_S``), with no bound on the sum. It outran
the 600 s dispatch deadline, every expiry left an orphaned dispatch, and three
of them made dev unhealthy.

The fix gives the hosted start command a wall-clock budget below that deadline,
read from a declared overlay value (``DOD_VERIFY_START_BUDGET_S``; the overlay
declares 540 s). Without a valid declared value the command refuses to start.
When the budget is spent the remaining checks are not run and the verdict is a
typed UNRESOLVED naming how many ran and how many did not; an unrun check is
never counted as passed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.dispatch_budget import (
    DISPATCH_DEADLINE_S,
    START_BUDGET_ENV,
    start_budget_s,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

_CHECK_COUNT = 12
_CHECK_SECONDS = 0.4
_BUDGET_SECONDS = 2.0
_DECLARED_BOUND_SECONDS = 2.5
_OVERLAY_VALUE_S = 540.0


def _items(command: str = f"sleep {_CHECK_SECONDS}") -> list[dict[str, Any]]:
    return [
        {
            "id": f"dod-slow-{n}",
            "description": f"slow check {n}",
            "checks": [{"check_type": "command", "command": command}],
        }
        for n in range(_CHECK_COUNT)
    ]


class _Collector(EvidenceCollector):
    """The real per-item runner over a fixed list of items."""

    items: list[dict[str, Any]] = []
    collected = 0

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
        execution_audience: EnumDodVerifyExecutionAudience = (
            EnumDodVerifyExecutionAudience.HOSTED
        ),
    ) -> list[ModelEvidenceCheckResult]:
        type(self).collected += 1
        return [self._check_evidence_item(i, ticket_id) for i in type(self).items]


def _run(
    items: list[dict[str, Any]],
    audience: EnumDodVerifyExecutionAudience = EnumDodVerifyExecutionAudience.HOSTED,
) -> ModelDodVerifyState:
    _Collector.items = items
    _Collector.collected = 0

    class _Handler(HandlerDodVerify):
        def _make_collector(self) -> EvidenceCollector:
            return _Collector()

    return _Handler()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id="OMN-25856", execution_audience=audience)
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.delenv("DOD_VERIFY_CHECK_TIMEOUT_S", raising=False)
    monkeypatch.setenv(START_BUDGET_ENV, str(_BUDGET_SECONDS))


def test_start_command_with_checks_over_the_budget_returns_inside_the_bound() -> None:
    started = time.monotonic()
    state = _run(_items())
    elapsed = time.monotonic() - started

    assert elapsed < _DECLARED_BOUND_SECONDS, (
        f"start command took {elapsed:.2f}s; unbounded sum of {_CHECK_COUNT} "
        f"checks is {_CHECK_COUNT * _CHECK_SECONDS:.1f}s"
    )
    assert state.status is EnumDodVerifyStatus.UNRESOLVED


def test_verdict_is_typed_unverifiable_and_names_ran_and_not_ran() -> None:
    state = _run(_items())

    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.DISPATCH_BUDGET_EXHAUSTED
    )
    assert state.budget_checks_run is not None
    assert state.budget_checks_not_run is not None
    assert state.budget_checks_run >= 1, "checks inside the budget still run"
    assert state.budget_checks_not_run >= 1
    assert state.budget_checks_run + state.budget_checks_not_run == _CHECK_COUNT
    assert state.error_message is not None
    assert (
        f"{state.budget_checks_run} of {_CHECK_COUNT} checks ran, "
        f"{state.budget_checks_not_run} did not"
    ) in state.error_message


def test_remaining_checks_are_reported_never_counted_as_passed() -> None:
    state = _run(_items())

    deferred = [
        c
        for c in state.checks
        if c.unverifiable_cause is EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED
    ]
    assert len(deferred) == state.budget_checks_not_run
    assert all(c.status is EnumEvidenceCheckStatus.SKIPPED for c in deferred)
    assert state.verified_count == state.budget_checks_run
    assert state.verified_count < _CHECK_COUNT
    assert state.status is not EnumDodVerifyStatus.VERIFIED


def test_refuses_to_start_without_a_declared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(START_BUDGET_ENV, raising=False)

    state = _run(_items())

    assert _Collector.collected == 0, "no check may start without a declared budget"
    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.DISPATCH_BUDGET_NOT_DECLARED
    )
    assert state.error_message is not None
    assert START_BUDGET_ENV in state.error_message


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", str(DISPATCH_DEADLINE_S), "900"])
def test_refuses_a_budget_that_is_not_below_the_dispatch_deadline(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(START_BUDGET_ENV, raw)

    assert start_budget_s() is None
    state = _run(_items())
    assert _Collector.collected == 0
    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.DISPATCH_BUDGET_NOT_DECLARED
    )


def test_overlay_value_540_is_accepted_and_a_fast_contract_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: inside the declared budget nothing is deferred."""
    monkeypatch.setenv(START_BUDGET_ENV, str(int(_OVERLAY_VALUE_S)))

    assert start_budget_s() == _OVERLAY_VALUE_S
    state = _run(_items("true"))

    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.budget_checks_not_run is None
    assert state.verified_count == _CHECK_COUNT


def test_a_failing_check_inside_the_budget_is_still_failed() -> None:
    """Known bad: a real red is a FAILED verdict, not an unverifiable one."""
    state = _run(_items("false")[:3])

    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.unresolved_cause is None


def test_local_done_gate_needs_no_declared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(START_BUDGET_ENV, raising=False)

    state = _run(_items("true")[:2], EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE)

    assert state.status is EnumDodVerifyStatus.VERIFIED
