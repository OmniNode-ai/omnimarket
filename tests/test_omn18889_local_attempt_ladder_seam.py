# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local delegate path persists its attempt ladder (OMN-18889).

THE DEFECT, measured 2026-09-20 against the live local store
(``~/.omninode/delegation/delegation.sqlite``): ``attempt_history`` is ``'[]'``
on all 281 modern rows and ``escalation_count`` is NULL on all 23,316. Two
distinct causes at one seam.

* ``LocalDelegationDispatchPort._project_evidence`` built the terminal payload
  with no ``attempts`` key, so the projection always reduced an EMPTY ladder.
  The ladder was already in hand at all three call sites -- the port appends a
  record per rung and already serializes that same list onto the CLI response.
  It simply never reached the evidence payload.
* ``escalation_count`` DOES survive the terminal model (it is inherited from
  ``ModelDelegateSkillResponse``); it is dropped one layer lower, because the
  terminal row builder never names it. Same for ``actual_score`` and
  ``required_bar``, which only the bus writer ``project()`` ever emitted.

WHY IT IS NOT MERELY A MISSING COLUMN. ``reduce_delegation_attempts`` is
ladder-authoritative: given a ladder it derives the terminal cause from the
rungs, and a ladder of quality-gate rejections correctly yields NO typed
cause. Only when the ladder is EMPTY does it fall through to sniffing the
error text for quota phrasing. The local path always handed it an empty
ladder, so the local path always took the text-sniffing branch -- which is how
a run whose rungs answered and were refused on quality could be recorded as a
provider quota failure. ``test_a_quality_gate_ladder_is_not_read_as_a_quota_failure``
below is that case, with the pre-fix behaviour as its positive control.

WHAT IS NOT BROKEN, asserted here so it is not "fixed" again. Prompt text,
response text and ``created_at`` are already persisted on this path and have
been since the local schema gained the columns on 2026-09-14. The 281-of-23,316
figure is schema age, not a code defect: coverage since that date is 281 of
281. ``TestTheTextAndTimestampAreAlreadyPersisted`` pins that, so the next
reader measures the cause before writing a no-op fix.

No mock stands in for the store: every test writes through the real
``SqliteDatabaseAdapter`` to a temporary file and reads the row back with
``sqlite3``.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as _port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

#: A message that WOULD type as a provider quota failure under the text
#: fallback in ``reduce_delegation_attempts``. Used to prove the ladder, not
#: the text, decides the cause once a ladder exists.
_QUOTA_SHAPED_TEXT = "quota exceeded on the upstream provider"


def _result(*, content: str | None = "an answer") -> ModelLlmDelegationCallResult:
    return ModelLlmDelegationCallResult(
        request_id="req-omn18889",
        success=content is not None,
        content=content,
        tokens_in=11,
        tokens_out=22,
        latency_ms=33,
        actual_cost_usd=Decimal("0"),
        savings_usd=Decimal("0.5"),
    )


def _attempt(
    *,
    tier: str,
    passed: bool,
    score: float,
    decision: str,
    reason: str,
) -> dict[str, object]:
    """One rung, carrying EVERY key the port appends.

    Deliberately the full set rather than a convenient subset. The first
    version of this helper carried eight of the fifteen, which is precisely
    why it stayed green while the real ladder refused the evidence write in
    CI: the three keys it omitted were the three the attempt record did not
    declare. ``TestEveryEmittedRungKeyIsDeclared`` holds this honest.
    """
    return {
        "tier": tier,
        "backend_id": f"backend-{tier}",
        "model_id": f"model-{tier}",
        "quality_gate_passed": passed,
        "quality_score": score,
        "cost_usd": 0.0,
        "failure_class": None,
        "error_message": "",
        "acceptance_decision": decision,
        "acceptance_reason": reason,
        "acceptance_detail": f"actual_score={score:.3f} required_bar=0.800",
        "input_tokens_measured": 120,
        "input_token_budget": 8000,
        "reasoning_preamble_rule": "no_boundary_found",
        "reasoning_preamble": "",
    }


def _project(
    tmp_path: Path,
    *,
    attempts: list[dict[str, object]],
    escalation_count: int,
    quality_passed: bool = True,
    failure_message: str = "",
    prompt: str = "the prompt as the customer typed it",
    content: str | None = "an answer",
    actual_score: float | None = None,
    required_bar: float | None = None,
) -> dict[str, Any]:
    """Run one terminal through the real port and real SQLite; return the row."""
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(evidence_db_path=db_path)
    correlation_id = uuid4()
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=_result(content=content),
        prompt=prompt,
        source_session_id=None,
        # OMN-18699 refuses a local evidence write with no tenant identity, so
        # every path into this helper names one rather than being exempted.
        tenant_id="omninode",
        quality_passed=quality_passed,
        failure_message=failure_message,
        cost_usd=Decimal("0"),
        savings_usd=Decimal("0.5"),
        escalation_count=escalation_count,
        attempts=attempts,
        actual_score=actual_score,
        required_bar=required_bar,
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
    assert found is not None, "the evidence write produced no row at all"
    row = dict(found)
    # The port swallows projection failures by design, so an empty row would
    # otherwise read as a pass. Prove the write really happened.
    assert row["task_type"] == "document"
    return row


class TestTheLadderReachesTheDurableRow:
    """AC1 and AC2."""

    def test_every_rung_is_persisted(self, tmp_path: Path) -> None:
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=False,
                    score=0.4,
                    decision="climb",
                    reason="deterministic_floor_failed",
                ),
                _attempt(
                    tier="cheap_cloud",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                ),
            ],
            escalation_count=1,
        )
        ladder = json.loads(row["attempt_history"])
        assert [entry["tier"] for entry in ladder] == ["local", "cheap_cloud"]
        assert [entry["acceptance_decision"] for entry in ladder] == [
            "climb",
            "accept",
        ]
        assert [entry["quality_score"] for entry in ladder] == [0.4, 0.9]

    def test_the_escalation_count_is_persisted(self, tmp_path: Path) -> None:
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=False,
                    score=0.4,
                    decision="climb",
                    reason="deterministic_floor_failed",
                ),
                _attempt(
                    tier="claude",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                ),
            ],
            escalation_count=1,
        )
        assert row["escalation_count"] == 1

    def test_a_single_rung_run_records_one_rung_and_no_escalation(
        self, tmp_path: Path
    ) -> None:
        """The common case. Before this change it recorded an empty ladder."""
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                )
            ],
            escalation_count=0,
        )
        assert len(json.loads(row["attempt_history"])) == 1
        assert row["escalation_count"] == 0

    def test_the_per_rung_score_is_readable_from_the_ladder(
        self, tmp_path: Path
    ) -> None:
        """The scores arrive INSIDE the ladder, which is what the eval consumer reads.

        The flat ``actual_score`` and ``required_bar`` columns were out of scope
        for the ladder half and stayed NULL; the score half now writes them.
        ``tests/test_omn18889_local_score_fields_seam.py`` owns that proof,
        through the real dispatch path. Here the flat column carries the value
        the call site passed, next to the per-rung score.
        """
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                )
            ],
            escalation_count=0,
            actual_score=0.9,
            required_bar=0.8,
        )
        assert json.loads(row["attempt_history"])[0]["quality_score"] == 0.9
        assert row["actual_score"] == 0.9
        assert row["required_bar"] == 0.8


class TestTheCauseComesFromTheLadder:
    """AC5. The reporting defect, and why the empty ladder caused it."""

    def test_a_quality_gate_ladder_is_not_read_as_a_quota_failure(
        self, tmp_path: Path
    ) -> None:
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=False,
                    score=0.43,
                    decision="climb",
                    reason="deterministic_floor_failed",
                ),
                _attempt(
                    tier="cheap_cloud",
                    passed=False,
                    score=0.43,
                    decision="climb",
                    reason="deterministic_floor_failed",
                ),
            ],
            escalation_count=1,
            quality_passed=False,
            failure_message=_QUOTA_SHAPED_TEXT,
            content=None,
        )
        assert row["terminal_failure_cause"] is None
        assert row["terminal_ok"] == 0

    def test_positive_control_an_empty_ladder_still_types_from_the_text(
        self, tmp_path: Path
    ) -> None:
        """The pre-fix path, reached deliberately.

        Without this the test above would also pass if the text fallback had
        simply been deleted, which would lose the bus paths that legitimately
        report no per-attempt detail.
        """
        row = _project(
            tmp_path,
            attempts=[],
            escalation_count=0,
            quality_passed=False,
            failure_message=_QUOTA_SHAPED_TEXT,
            content=None,
        )
        assert row["terminal_failure_cause"] == "provider_quota_exhausted"


class TestTheTextAndTimestampAreAlreadyPersisted:
    """Not a fix. A guard on the three fields the brief believed were missing."""

    def test_the_prompt_the_response_and_created_at_are_all_written(
        self, tmp_path: Path
    ) -> None:
        row = _project(
            tmp_path,
            attempts=[
                _attempt(
                    tier="local",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                )
            ],
            escalation_count=0,
            prompt="the prompt as the customer typed it",
            content="the model's full answer",
        )
        assert row["prompt_text"] == "the prompt as the customer typed it"
        assert row["response_text"] == "the model's full answer"
        assert row["created_at"] is not None
        assert row["timestamp"] is not None


class TestTheEvidenceWriteIsStillBestEffort:
    def test_an_unwritable_evidence_target_does_not_raise(self, tmp_path: Path) -> None:
        """The delegation answer must never be lost to an evidence failure.

        Adding fields to the payload must not turn a swallowed projection
        error into a raised one.
        """
        unwritable = tmp_path / "missing-directory" / "delegation.sqlite"
        port = LocalDelegationDispatchPort(evidence_db_path=unwritable)
        port._project_evidence(
            correlation_id=UUID("11111111-1111-1111-1111-111111111111"),
            task_type="document",
            endpoint_ref="local",
            model_id="model-local",
            result=_result(),
            prompt="p",
            source_session_id=None,
            tenant_id="omninode",
            quality_passed=True,
            failure_message="",
            cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
            escalation_count=0,
            attempts=[
                _attempt(
                    tier="local",
                    passed=True,
                    score=0.9,
                    decision="accept",
                    reason="quality_bar_met",
                )
            ],
            actual_score=0.9,
            required_bar=0.8,
        )


class TestEveryEmittedRungKeyIsDeclared:
    """The guard for the way this change first broke, in CI rather than locally.

    Routing the ladder through the typed terminal projection made
    ``ModelDelegateSkillAttemptRecord`` authoritative over the per-rung dicts
    for the first time. The port had long appended three keys the model does
    not declare -- they reached the CLI response regardless, because that
    payload is assembled as raw dicts and never validated -- and the model
    forbids extras, so the FIRST real ladder refused the whole evidence write
    and the row silently did not materialize. The seam test above did not
    catch it, because its hand-written rung carried only the keys the model
    already had: a fixture that is a subset of production is not a fixture.

    This reads the append sites out of the port's own source, so a key added
    to a rung tomorrow turns this red at once rather than after the next
    evidence write disappears.
    """

    @staticmethod
    def _emitted_keys() -> set[str]:
        tree = ast.parse(Path(_port_module.__file__).read_text(encoding="utf-8"))
        keys: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "append"):
                continue
            target = func.value
            if not (isinstance(target, ast.Name) and target.id == "attempts"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Dict)):
                continue
            keys.update(
                key.value
                for key in node.args[0].keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        return keys

    def test_the_parser_finds_the_append_sites(self) -> None:
        """Positive control: an empty key set would make the next test vacuous."""
        assert len(self._emitted_keys()) >= 10

    def test_no_emitted_key_is_undeclared(self) -> None:
        undeclared = self._emitted_keys() - set(
            ModelDelegateSkillAttemptRecord.model_fields
        )
        assert not undeclared, (
            "the port appends per-rung keys the attempt record does not declare, "
            "and the model forbids extras, so the evidence write will be refused "
            f"and swallowed: {sorted(undeclared)}"
        )
