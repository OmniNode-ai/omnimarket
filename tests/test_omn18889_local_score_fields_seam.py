# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local delegate path persists ``actual_score`` and ``required_bar`` (OMN-18889).

THE DEFECT, measured 2026-09-24T01:1xZ against the live local store
(``~/.omninode/delegation/delegation.sqlite``): 23,603 rows, 0 with a non-null
``actual_score`` and 0 with a non-null ``required_bar``. The ladder half of this
ticket (omnimarket#2716) made the per-rung scores reach ``attempt_history``; the
two flat columns a range check reads stayed NULL on every local row.

Two causes, one seam, the same shape as the escalation count before it:

* ``LocalDelegationDispatchPort._project_evidence`` put neither value in the
  terminal payload, although the gate's graded score and the task class's
  declared bar were both in hand at every scored call site.
* ``ModelDelegateSkillTerminalProjection`` is ``extra="ignore"`` and declared
  neither field, so a payload that DID carry them lost them silently, and the
  terminal row builder never named either column.

THE RULE THIS PINS (unified plan row G2): a scored terminal writes real numbers,
equal to the score and bar the terminal itself reports; a terminal that was
never scored (a transport failure) writes NULL in both, and never zero. The
response model's ``quality_score`` defaults to ``0.0``, so reading the score off
that field would turn every unscored terminal into a graded zero and poison the
response-quality baseline (G4) that reads these columns. The tests below hold
both halves: the known-bad input (an unscored terminal) and the valid one.

No mock stands in for the store: every dispatch writes through the real
``SqliteDatabaseAdapter`` to a temporary file and the row is read back with
``sqlite3`` by correlation id. Only the network transport is patched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.routing import delegation_backend_resolution
from tests.fixtures.judge_inference import CannedAdequacyBridge

pytestmark = pytest.mark.unit

_GOOD_RESEARCH_ANSWER = (
    "According to Smith (2020) and the theorem in section 3, the tradeoff "
    "is significant because the evidence shows X; therefore we conclude Y. "
    "See references [12] for the methodical analysis and the risk profile "
    "this approach carries in practice."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    """Same reason as the sibling evidence module: a cached probe verdict for
    the shared fixture URL would short-circuit this file's patched probe."""
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture
def research_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """One local rung only, so a refusal or a transport failure is terminal."""
    backends = [
        {
            "backend_id": "local-research",
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["research"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, *, content: str, status_code: int = 200
) -> None:
    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        body: dict[str, Any] = (
            {
                "choices": [{"message": {"content": f"### ANSWER\n{content}"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            }
            if status_code == 200
            else {"error": {"message": "upstream exploded", "code": status_code}}
        )
        return transport.ModelTransportResponse(
            status_code=status_code, json_body=body, latency_ms=42
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def _dispatch(db_path: Path, correlation_id: UUID) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(
            inference_bridge=CannedAdequacyBridge(adequacy_score=0.95)
        ),
    )
    result: dict[str, Any] = asyncio.run(
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
            tenant_id=None,
        )
    )
    return result


def _row(db_path: Path, correlation_id: UUID) -> dict[str, Any]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    assert found is not None, "the evidence write produced no row at all"
    return dict(found)


def _declared_bar() -> float:
    return resolve_required_bar_authority(task_type="research").required_bar


class TestAScoredTerminalWritesRealNumbers:
    """The valid half: the row carries the terminal's own score and bar."""

    def test_an_accepted_delegation_persists_its_score_and_bar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research_backend: None
    ) -> None:
        _patch_transport(monkeypatch, content=_GOOD_RESEARCH_ANSWER)
        db_path = tmp_path / "delegation.sqlite"
        correlation_id = uuid4()

        result = _dispatch(db_path, correlation_id)
        row = _row(db_path, correlation_id)

        assert result["status"] == "completed"
        assert row["actual_score"] is not None
        assert row["required_bar"] is not None
        assert row["actual_score"] == pytest.approx(result["quality_score"])
        assert row["required_bar"] == pytest.approx(_declared_bar())

    def test_a_quality_refused_delegation_persists_its_score_and_bar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research_backend: None
    ) -> None:
        """A refusal was graded; the grade is evidence, not an absence."""
        _patch_transport(monkeypatch, content=_REFUSAL)
        db_path = tmp_path / "delegation.sqlite"
        correlation_id = uuid4()

        result = _dispatch(db_path, correlation_id)
        row = _row(db_path, correlation_id)

        assert result["status"] == "failed"
        assert result["quality_gate_passed"] is False
        assert row["actual_score"] is not None
        assert row["actual_score"] == pytest.approx(result["quality_score"])
        assert row["required_bar"] == pytest.approx(_declared_bar())


class TestAnUnscoredTerminalWritesNullNeverZero:
    """The known-bad half: a transport failure has no grade to record."""

    def test_a_transport_failure_writes_null_in_both_columns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research_backend: None
    ) -> None:
        db_path = tmp_path / "delegation.sqlite"
        # Materialize both columns first with a scored row, so the NULL below
        # is a written NULL and not a column the adapter never created.
        _patch_transport(monkeypatch, content=_GOOD_RESEARCH_ANSWER)
        control_id = uuid4()
        _dispatch(db_path, control_id)
        control = _row(db_path, control_id)
        assert control["actual_score"] is not None, "positive control"

        _patch_transport(monkeypatch, content="", status_code=500)
        correlation_id = uuid4()
        result = _dispatch(db_path, correlation_id)
        row = _row(db_path, correlation_id)

        assert result["status"] == "failed"
        assert "actual_score" in row
        assert "required_bar" in row
        assert row["actual_score"] is None
        assert row["required_bar"] is None


class TestAnUnscoredTerminalDoesNotEraseAnEarlierScore:
    """A later unscored terminal for the same correlation leaves the score."""

    def test_a_graded_zero_survives_a_later_unscored_terminal(
        self, tmp_path: Path
    ) -> None:
        from decimal import Decimal

        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        db_path = tmp_path / "delegation.sqlite"
        db = SqliteDatabaseAdapter(db_path)
        correlation_id = uuid4()
        base: dict[str, object] = {
            "correlation_id": str(correlation_id),
            "task_type": "research",
            "tenant_id": "omninode",
            "metrics": {"cost_usd": float(Decimal("0"))},
        }
        handler = HandlerProjectionDelegation()
        handler.project_delegate_skill_terminal(
            ModelDelegateSkillTerminalProjection.from_payload(
                {**base, "status": "failed", "actual_score": 0.0, "required_bar": 0.8}
            ),
            db,
        )
        handler.project_delegate_skill_terminal(
            ModelDelegateSkillTerminalProjection.from_payload(
                {**base, "status": "failed"}
            ),
            db,
        )
        row = _row(db_path, correlation_id)
        assert row["actual_score"] == 0.0
        assert row["required_bar"] == pytest.approx(0.8)


class TestTheTerminalProjectionModelDeclaresBothFields:
    """The seam contract (plan seam G.1): the model no longer drops them."""

    _BASE: dict[str, object] = {
        "status": "completed",
        "correlation_id": "11111111-1111-1111-1111-111111111111",
        "task_type": "research",
    }

    def test_declared_scores_survive_validation(self) -> None:
        terminal = ModelDelegateSkillTerminalProjection.from_payload(
            {**self._BASE, "actual_score": 0.91, "required_bar": 0.8}
        )
        assert terminal.actual_score == pytest.approx(0.91)
        assert terminal.required_bar == pytest.approx(0.8)

    def test_an_unscored_payload_is_none_not_the_quality_score_default(
        self,
    ) -> None:
        """``quality_score`` defaults to 0.0; the projection must not read it."""
        terminal = ModelDelegateSkillTerminalProjection.from_payload(self._BASE)
        assert terminal.quality_score == 0.0
        assert terminal.actual_score is None
        assert terminal.required_bar is None

    def test_an_out_of_range_score_is_refused(self) -> None:
        with pytest.raises(ValueError, match="actual_score"):
            ModelDelegateSkillTerminalProjection.from_payload(
                {**self._BASE, "actual_score": 1.5}
            )
