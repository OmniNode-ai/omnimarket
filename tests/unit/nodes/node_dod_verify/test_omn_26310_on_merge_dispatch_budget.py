# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26310: a merge-event dod_verify dispatch is bounded as a whole.

Measured on the dev runtime (omni-201, 2026-11-23): ``node_dod_verify``
dispatches for ``onex.evt.github.pr-merged.v1`` offsets 6659, 6660 and 6661 took
948 s, 942 s and 810 s against the 600 s dispatch deadline. The collector bounds
each external call on its own (30 s per check command, 300 s per git operation,
900 s per lock-exact environment build) but nothing bounds the SUM, so a
contract with enough checks, or three project roots to build, outruns the
deadline.

The fix is a wall-clock budget for the whole on-merge dispatch. A check the
budget cannot cover is not run; it is recorded SKIPPED with
``CHECK_BUDGET_EXCEEDED`` (never a pass, still blocks a Done flip), and the
scheduled closeout sweep verifies it without a dispatch deadline.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify_on_merge import (
    HandlerDodVerifyOnMerge,
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
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

_BUDGET_ENV = "DOD_VERIFY_ON_MERGE_BUDGET_S"
# 12 checks of 0.4 s each: 4.8 s when nothing bounds the sum, each one far
# under the 30 s per-check ceiling, so no single check is ever "slow".
_CHECK_COUNT = 12
_CHECK_SECONDS = 0.4
_BUDGET_SECONDS = 2.0
# Checks start while >= 1 s of the 2 s budget is left, so about three run; the
# rest are deferred. Well under the 4.8 s unbounded sum.
_DECLARED_BOUND_SECONDS = 2.5


def _items() -> list[dict[str, Any]]:
    return [
        {
            "id": f"dod-slow-{n}",
            "description": f"slow check {n}",
            "checks": [{"check_type": "command", "command": f"sleep {_CHECK_SECONDS}"}],
        }
        for n in range(_CHECK_COUNT)
    ]


class _SerialChecksCollector(EvidenceCollector):
    """The real collector's per-item runner, over a fixed list of slow items."""

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
        execution_audience: EnumDodVerifyExecutionAudience = (
            EnumDodVerifyExecutionAudience.HOSTED
        ),
    ) -> list[ModelEvidenceCheckResult]:
        return [self._check_evidence_item(item, ticket_id) for item in _items()]


class _SlowChecksVerifier:
    """HandlerDodVerify, whose collector runs the slow items the real way."""

    def handle(self, payload: ModelDodVerifyStartCommand) -> ModelDodVerifyState:
        class _Handler(HandlerDodVerify):
            def _make_collector(self) -> EvidenceCollector:
                return _SerialChecksCollector()

        return _Handler()._handle_typed(payload)


def _merge() -> ModelPrMergedEvent:
    return ModelPrMergedEvent(
        event_id="evt-26310",
        repo="OmniNode-ai/omnimarket",
        branch="omn-26310-dod-verify-dispatch-budget",
        pr_number=1,
        ticket="OMN-26310",
        merged_at=datetime(2026, 11, 23, 8, 0, 0, tzinfo=UTC).isoformat(),
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    monkeypatch.delenv("DOD_VERIFY_CHECK_TIMEOUT_S", raising=False)
    monkeypatch.setenv(_BUDGET_ENV, str(_BUDGET_SECONDS))


def test_on_merge_dispatch_with_slow_checks_returns_inside_the_declared_bound() -> None:
    """The defect: sum of in-bound checks outruns the dispatch deadline."""
    handler = HandlerDodVerifyOnMerge(verifier=_SlowChecksVerifier())

    started = time.monotonic()
    state = handler.handle(_merge())
    elapsed = time.monotonic() - started

    assert state is not None
    assert elapsed < _DECLARED_BOUND_SECONDS, (
        f"dispatch took {elapsed:.2f}s; unbounded sum of {_CHECK_COUNT} checks "
        f"is {_CHECK_COUNT * _CHECK_SECONDS:.1f}s"
    )


def test_checks_the_budget_cannot_cover_are_deferred_typed_never_passed() -> None:
    handler = HandlerDodVerifyOnMerge(verifier=_SlowChecksVerifier())

    state = handler.handle(_merge())

    assert state is not None
    deferred = [
        c
        for c in state.checks
        if c.unverifiable_cause is EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED
    ]
    ran = [c for c in state.checks if c.status is EnumEvidenceCheckStatus.VERIFIED]
    assert deferred, "some checks must be deferred with the typed budget cause"
    assert ran, "checks inside the budget still run (positive control)"
    assert all(c.status is EnumEvidenceCheckStatus.SKIPPED for c in deferred)
    assert all("DISPATCH_BUDGET_EXCEEDED" in c.message for c in deferred)
    # An unrun check is never a pass: the verdict cannot be VERIFIED.
    assert state.status is not EnumDodVerifyStatus.VERIFIED


def test_no_budget_outside_the_on_merge_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start command (local Done gate, sweep) is not budgeted: all checks run."""
    monkeypatch.setenv(_BUDGET_ENV, "0.1")
    collector = _SerialChecksCollector()

    results = collector.collect("OMN-26310")

    assert all(r.status is EnumEvidenceCheckStatus.VERIFIED for r in results)


def test_environment_build_ceiling_is_clamped_to_the_budget_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 900 s ``uv sync`` ceiling cannot outlive the dispatch's budget."""
    import subprocess

    from omnimarket.nodes.node_dod_verify.services import evidence_collector
    from omnimarket.nodes.node_dod_verify.services.dispatch_budget import (
        dispatch_budget,
    )

    project = tmp_path / "proj"
    project.mkdir()
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    seen: list[float] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(evidence_collector.subprocess, "run", _fake_run)
    monkeypatch.delenv("DOD_VERIFY_HERMETIC_SYNC_TIMEOUT_S", raising=False)

    collector = EvidenceCollector()
    with dispatch_budget(30.0):
        path, err = collector._ensure_hermetic_uv_env(project)
    assert path is None
    assert err is not None
    assert seen
    assert seen[0] <= 30.0, f"uv sync was given {seen[0]}s inside a 30 s budget"

    # Positive control: with no budget open the declared ceiling is untouched.
    seen.clear()
    unbudgeted = EvidenceCollector()
    unbudgeted._ensure_hermetic_uv_env(project)
    assert seen == [900.0]
