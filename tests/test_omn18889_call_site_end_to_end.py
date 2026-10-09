# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three evidence call sites hand the ladder and the count to the row (OMN-18889).

``tests/test_omn18889_local_attempt_ladder_seam.py`` calls
``LocalDelegationDispatchPort._project_evidence`` directly with the ladder and
the escalation count passed in. That proves the method writes what it is
given. It does not prove the defect OMN-18889 names, which was the CALL SITE
dropping them: all three call sites in ``LocalDelegationDispatchPort.dispatch``
could pass ``attempts=[]`` and ``escalation_count=0`` and every one of those
tests stayed green.

These tests drive ``dispatch`` itself, end to end, with a delegation that
climbs the ladder, and read the stored row back with ``sqlite3``. One scenario
per call site, because each is a separate ``self._project_evidence(...)``
statement:

* ``TestTransportFailureTerminal``: the ladder is exhausted by a provider
  failure (the call-site comment "Cannot escalate ... terminal FAILED").
* ``TestQualityPassTerminal``: a higher rung passes the gate (the accepted
  terminal).
* ``TestQualityFailTerminal``: every rung is refused by the gate and the
  escalation budget runs out.

Every scenario has at least two rungs and ``escalation_count == 1``. A call
site that passes an empty ladder or ``escalation_count=0`` turns each of them
red; the falsifier is recorded on the PR that adds this file.

What is real: the port, its escalation loop, the quality gate, the typed
terminal projection and the SQLite store. What is stubbed: the routing
authority (a fixed three-tier ladder) and the effect handler (a canned answer
per tier), exactly as ``test_local_dispatch_escalation_omn13849.py`` does.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as evaluate_quality_gate,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = pytest.mark.unit

_LADDER: tuple[str, ...] = ("local", "cheap_cloud", "claude")
_TIER_BACKEND_ID: dict[str, str] = {
    "local": "local-coder",
    "cheap_cloud": "cloud-glm",
    "claude": "cloud-gemini-2-5-flash",
}
_BACKEND_TIER: dict[str, str] = {v: k for k, v in _TIER_BACKEND_ID.items()}
_TIER_MODEL: dict[str, str] = {
    "local": "Qwen3.6-35B-A3B",
    "cheap_cloud": "glm-5.2",
    "claude": "gemini-2.5-flash",
}

#: Text the quota fallback in ``reduce_delegation_attempts`` would type as a
#: provider capacity failure if it were ever consulted for a quality refusal.
_QUOTA_SHAPED_TEXT = "quota exceeded on the upstream provider"

_GOOD_RESEARCH = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


def _backend_for_tier(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_TIER_BACKEND_ID[tier],
        model_id=_TIER_MODEL[tier],
        endpoint_ref=f"https://{tier}.example/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch, *, max_escalations: int) -> None:
    """Fix the routing authority to local -> cheap_cloud -> claude, one backend each."""

    def fake_resolve(
        task_type: str, *, backend_id: str | None = None
    ) -> ModelResolvedDelegationBackend:
        if backend_id is None:
            return _backend_for_tier("local")
        return _backend_for_tier(_BACKEND_TIER[backend_id])

    def fake_next_eligible_tier(
        current: str,
        excluded: frozenset[str],
        **_kwargs: object,
    ) -> str | None:
        try:
            idx = _LADDER.index(current)
        except ValueError:
            return None
        for tier in _LADDER[idx + 1 :]:
            if tier not in excluded:
                return tier
        return None

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", fake_resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", fake_next_eligible_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda _t, **_kw: "local")
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda tier, _t, **_kw: _TIER_BACKEND_ID[tier]
    )
    monkeypatch.setattr(
        port_mod, "tier_for_backend", lambda backend_id: _BACKEND_TIER.get(backend_id)
    )
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda _t, _tt, _x: None
    )
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _t: max_escalations
    )
    # Every tier is metered here, so retry-local on a free tier stays inert and
    # the run climbs the ladder instead of re-asking the same rung.
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)


class _Effect:
    """A canned answer per tier; a tier in ``fail_tiers`` fails at the transport."""

    def __init__(
        self,
        *,
        pass_tiers: frozenset[str] = frozenset(),
        fail_tiers: frozenset[str] = frozenset(),
        failure_class: EnumDelegationFailureClass = (
            EnumDelegationFailureClass.RATE_LIMITED
        ),
    ) -> None:
        self._pass_tiers = pass_tiers
        self._fail_tiers = fail_tiers
        self._failure_class = failure_class
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        tier = request.model_tier
        self.calls.append(tier)
        if tier in self._fail_tiers:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=self._failure_class,
                # Deliberately not quota-shaped: the only way the row can type
                # this run as a capacity failure is by reading the ladder.
                error_message="the upstream declined the call",
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_RESEARCH if tier in self._pass_tiers else _REFUSAL,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0.01"),
            savings_usd=Decimal("0"),
        )


def _run(
    tmp_path: Path, effect: _Effect
) -> tuple[dict[str, object], dict[str, Any], list[dict[str, Any]]]:
    """Dispatch through the real port; return (response, stored row, stored ladder)."""
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    correlation_id = uuid4()
    response = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            # OMN-18699 refuses a local evidence write with no tenant identity.
            tenant_id="omninode",
        )
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    # The port swallows an evidence failure by design, so a missing row would
    # otherwise read as a pass.
    assert found is not None, "dispatch wrote no evidence row at all"
    row = dict(found)
    return response, row, json.loads(row["attempt_history"])


def _assert_climbed_ladder_is_on_the_row(
    response: dict[str, object],
    row: dict[str, Any],
    ladder: list[dict[str, Any]],
    *,
    effect: _Effect,
) -> None:
    """AC1 and AC2: one element per rung actually tried, and the climb count."""
    assert effect.calls == ["local", "cheap_cloud"], "the scenario did not climb"
    assert response["escalation_count"] == 1
    # AC2: the stored count is the one the run reported, not NULL and not 0.
    assert row["escalation_count"] == 1
    # AC1: one element per rung, in the order they were tried.
    assert [rung["tier"] for rung in ladder] == ["local", "cheap_cloud"]
    assert [rung["backend_id"] for rung in ladder] == ["local-coder", "cloud-glm"]
    reported = response["attempts"]
    assert isinstance(reported, list)
    assert len(ladder) == len(reported)


class TestTransportFailureTerminal:
    """Call site 1: the ladder ends on a provider failure it cannot climb past."""

    def test_the_ladder_and_count_reach_the_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # local is refused by the gate (a climb); cheap_cloud then fails at the
        # transport with the escalation budget (1) already spent: terminal.
        _install_ladder(monkeypatch, max_escalations=1)
        effect = _Effect(fail_tiers=frozenset({"cheap_cloud"}))
        response, row, ladder = _run(tmp_path, effect)

        assert response["status"] == "failed"
        _assert_climbed_ladder_is_on_the_row(response, row, ladder, effect=effect)
        assert ladder[1]["failure_class"] == "rate_limited"

    def test_the_cause_is_resolved_from_the_stored_ladder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5. The failure text carries no quota phrasing, so only the ladder can type it.

        The gate refused the local rung before cheap_cloud hit its rate limit,
        so the gate decided the run and the later capacity refusal does not
        (OMN-19004, OMN-19448).
        """
        _install_ladder(monkeypatch, max_escalations=1)
        effect = _Effect(fail_tiers=frozenset({"cheap_cloud"}))
        _response, row, _ladder = _run(tmp_path, effect)

        assert row["terminal_ok"] == 0
        assert row["terminal_failure_cause"] == "quality_gate_refused"


class TestQualityPassTerminal:
    """Call site 2: a higher rung passes the gate after a lower one was refused."""

    def test_the_ladder_and_count_reach_the_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_ladder(monkeypatch, max_escalations=2)
        effect = _Effect(pass_tiers=frozenset({"cheap_cloud"}))
        response, row, ladder = _run(tmp_path, effect)

        assert response["status"] == "completed"
        _assert_climbed_ladder_is_on_the_row(response, row, ladder, effect=effect)
        assert [rung["quality_gate_passed"] for rung in ladder] == [False, True]
        assert row["terminal_ok"] == 1
        assert row["terminal_failure_cause"] is None

    def test_a_first_rung_pass_records_one_rung_and_no_climb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the same site, no climb, so the assertions above are not constants."""
        _install_ladder(monkeypatch, max_escalations=2)
        effect = _Effect(pass_tiers=frozenset({"local"}))
        response, row, ladder = _run(tmp_path, effect)

        assert effect.calls == ["local"]
        assert response["escalation_count"] == 0
        assert row["escalation_count"] == 0
        assert [rung["tier"] for rung in ladder] == ["local"]


class TestQualityFailTerminal:
    """Call site 3: every rung is refused by the gate and the budget runs out."""

    def test_the_ladder_and_count_reach_the_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_ladder(monkeypatch, max_escalations=1)
        effect = _Effect()
        response, row, ladder = _run(tmp_path, effect)

        assert response["status"] == "failed"
        _assert_climbed_ladder_is_on_the_row(response, row, ladder, effect=effect)
        assert [rung["quality_gate_passed"] for rung in ladder] == [False, False]

    def test_a_quality_refusal_is_not_read_as_a_quota_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5. A refused ladder is typed by the gate, even when the failure TEXT looks like quota.

        The real gate's failure text is wrapped to carry quota phrasing, the
        way a model that quotes a provider error in its answer would make it.
        With the ladder on the row the cause is ``quality_gate_refused``
        (OMN-19448); with an empty ladder the text fallback would type it
        ``provider_quota_exhausted``.
        """
        _install_ladder(monkeypatch, max_escalations=1)
        real_gate = evaluate_quality_gate

        def quota_worded_gate(*args: Any, **kwargs: Any) -> Any:
            verdict = real_gate(*args, **kwargs)
            if verdict.passed:
                return verdict
            return verdict.model_copy(
                update={
                    "failure_reasons": (*verdict.failure_reasons, _QUOTA_SHAPED_TEXT)
                }
            )

        monkeypatch.setattr(port_mod, "evaluate_quality_gate", quota_worded_gate)
        effect = _Effect()
        _response, row, _ladder = _run(tmp_path, effect)

        assert _QUOTA_SHAPED_TEXT in row["quality_gate_detail"]
        assert row["terminal_ok"] == 0
        assert row["terminal_failure_cause"] == "quality_gate_refused"
