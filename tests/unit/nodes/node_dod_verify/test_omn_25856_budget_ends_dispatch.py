# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-25856: a spent start budget ends the dispatch before the 600 s deadline.

Measured on the dev runtime (2026-11-26): the dispatch begun 06:40:12Z logged
DISPATCH_BUDGET_EXCEEDED at the 540 s budget and still ran to 612 s. The budget
clamped the check subprocesses and the environment builds, but the steps around
a check kept their own ceilings: the live-PR ``gh`` reads, the must-fail control,
the durable-gate ``git`` reads and the Linear read. Their ceilings summed past
the 60 s of headroom. Every subprocess a dispatch starts is now clamped to the
budget, and a check that finished at the budget's edge starts no follow-up step.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as gh_effect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services import dispatch_budget
from omnimarket.nodes.node_dod_verify.services.dispatch_budget import (
    dispatch_budget as open_budget,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

_BUDGET_S = 1.5
_SLOW_S = 8


def test_gh_json_is_cut_at_the_budget() -> None:
    started = time.monotonic()
    with open_budget(_BUDGET_S):
        data, _detail = gh_effect._gh_json(["sleep", str(_SLOW_S)], 30)
    assert data is None
    assert time.monotonic() - started < _BUDGET_S + 2.0


def test_gh_json_lines_is_cut_at_the_budget() -> None:
    started = time.monotonic()
    with open_budget(_BUDGET_S):
        items, _detail = gh_effect._gh_json_lines(["sleep", str(_SLOW_S)], 30)
    assert items is None
    assert time.monotonic() - started < _BUDGET_S + 2.0


def test_gate_git_ceiling_is_clamped_to_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[float] = []

    def fake_run(*_a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(gh_effect.subprocess, "run", fake_run)
    with open_budget(_BUDGET_S):
        gh_effect._gate_git(str(tmp_path), "status")
    assert seen
    assert seen[0] <= _BUDGET_S


def test_gate_git_ceiling_is_unchanged_without_a_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: the local Done gate keeps its own ceiling."""
    seen: list[float] = []

    def fake_run(*_a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(gh_effect.subprocess, "run", fake_run)
    gh_effect._gate_git(str(tmp_path), "status")
    assert seen == [gh_effect._GATE_GIT_TIMEOUT_S]


class _Collector(EvidenceCollector):
    followups = 0

    def _check_evidence_item(  # type: ignore[override]
        self, item: Any, ticket_id: str, contract_path: Path | None = None
    ) -> ModelEvidenceCheckResult:
        return ModelEvidenceCheckResult(
            evidence_id="e",
            description="d",
            status=EnumEvidenceCheckStatus.VERIFIED,
            message="ok",
        )

    def _live_pr_checks_for_item(self, *_a: Any, **_k: Any) -> list[Any]:
        type(self).followups += 1
        return []

    def _apply_must_fail_control(self, *a: Any, **_k: Any) -> list[Any]:
        type(self).followups += 1
        return list(a[3])


def _run_item(budget_s: float | None) -> list[ModelEvidenceCheckResult]:
    _Collector.followups = 0
    item = {"id": "e", "description": "d", "checks": []}
    from omnimarket.enums.enum_dod_verify_execution_audience import (
        EnumDodVerifyExecutionAudience,
    )

    collector = _Collector()
    if budget_s is None:
        return collector._execute_item(
            item,
            "OMN-25856",
            None,
            0,
            EnumDodVerifyExecutionAudience.HOSTED,
            None,
            None,
        )
    with open_budget(budget_s):
        time.sleep(budget_s + 0.2)
        assert dispatch_budget.budget_exhausted()
        return collector._execute_item(
            item,
            "OMN-25856",
            None,
            0,
            EnumDodVerifyExecutionAudience.HOSTED,
            None,
            None,
        )


def test_a_spent_budget_starts_no_followup_step_and_no_green() -> None:
    results = _run_item(1.0)
    assert _Collector.followups == 0
    assert len(results) == 1
    assert results[0].status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        results[0].unverifiable_cause
        is EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED
    )


def test_followup_steps_still_run_inside_the_budget() -> None:
    """Positive control: with no budget open the follow-up steps run."""
    results = _run_item(None)
    assert _Collector.followups == 2
    assert results[0].status is EnumEvidenceCheckStatus.VERIFIED
