# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""events: one judged-acceptance event per validated primary verdict, built purely."""

import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_origin import (
    EnumAcceptanceOrigin,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_event_call import (
    ModelAcceptanceEventCall,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_event_judge import (
    ModelAcceptanceEventJudge,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_run_status import (
    EnumAcceptanceRunStatus,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_agreement import (
    ModelAcceptanceAgreement,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_result import (
    ModelAcceptanceJudgeResult,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    EDIT_LOOP_TASK,
    item,
    run,
    verdict,
)

pytestmark = pytest.mark.unit

_NS = UUID("00000000-0000-4000-8000-000000000001")
_PROBE_TASK = "say ok"
_AGREEMENT = ModelAcceptanceAgreement(
    double_judged=10,
    required_double_judged=10,
    agreed=9,
    agreement=0.9,
    kappa=0.78,
    kappa_min=0.6,
)
_JUDGE = ModelAcceptanceEventJudge(
    tenant_id=_NS,
    judge_run_id="judge-run-1",
    judge_model="judge-model",
    judge_model_version="v1",
    rubric_id="delegation_acceptance",
    rubric_hash="abc123",
    calibration_run_id="calibration-1",
    judged_at=datetime(2026, 11, 1, tzinfo=UTC),
    origin=EnumAcceptanceOrigin.LIVE,
)


def _call(
    item_id: str, model_key: str = "model-a", tier: str = "local"
) -> ModelAcceptanceEventCall:
    return ModelAcceptanceEventCall(
        item_id=item_id,
        correlation_id=uuid5(_NS, item_id),
        delegated_model_key=model_key,
        delegated_tier=tier,
        call_time=datetime(2026, 10, 31, tzinfo=UTC),
    )


def _events(
    items: list[ModelAcceptanceItem],
    verdicts: list[ModelAcceptanceVerdict],
    calls: list[ModelAcceptanceEventCall],
) -> ModelAcceptanceJudgeResult:
    return run(
        operation=EnumAcceptanceOperation.EVENTS,
        items=tuple(items),
        primary_verdicts=tuple(verdicts),
        agreement=_AGREEMENT,
        event_calls=tuple(calls),
        event_judge=_JUDGE,
    )


def _three() -> tuple[
    list[ModelAcceptanceItem],
    list[ModelAcceptanceVerdict],
    list[ModelAcceptanceEventCall],
]:
    items = [
        item("i1"),
        item("i2", task_type="code", task=EDIT_LOOP_TASK),
        item("i3", model="model-b"),
    ]
    return (
        items,
        [verdict("i1", True), verdict("i2", False), verdict("i3", True)],
        [_call("i1"), _call("i2", tier="cloud"), _call("i3", "model-b")],
    )


def test_events_one_per_verdict() -> None:
    result = _events(*_three())
    assert result.status is EnumAcceptanceRunStatus.PASSED
    assert result.issues == ()
    events = result.judged_events
    assert [e.delegated_model_key for e in events] == ["model-a", "model-a", "model-b"]
    assert [e.delegated_tier for e in events] == ["local", "cloud", "local"]
    assert [e.kind for e in events] == ["task", "edit_loop_turn", "task"]
    assert [e.accept for e in events] == [True, False, True]
    assert {e.calibration_n for e in events} == {10}
    assert {e.calibration_agreement for e in events} == {0.9}
    assert {e.calibration_kappa for e in events} == {0.78}
    assert len({e.event_id for e in events}) == 3
    assert {e.rubric_version for e in events} == {result.rubric_version}


def test_events_probe_excluded() -> None:
    items, verdicts, calls = _three()
    items.append(item("p1", task=_PROBE_TASK))
    calls.append(_call("p1"))
    result = _events(items, verdicts, calls)
    assert result.status is EnumAcceptanceRunStatus.PASSED
    assert [e.correlation_id for e in result.judged_events] == [
        uuid5(_NS, i) for i in ("i1", "i2", "i3")
    ]


def test_events_missing_model_refused() -> None:
    items, verdicts, calls = _three()
    calls[1] = _call("i2", model_key="")
    result = _events(items, verdicts, calls)
    assert result.status is EnumAcceptanceRunStatus.FAILED
    assert result.judged_events == ()
    assert [(i.code, i.item_id) for i in result.issues] == [
        (Code.MODEL_KEY_MISSING, "i2")
    ]


def test_events_refuse_missing_call_verdict_and_calibration() -> None:
    items, verdicts, calls = _three()
    result = _events(items, verdicts[:2], calls[:2])
    assert {(i.code, i.item_id) for i in result.issues} == {
        (Code.PRIMARY_VERDICT_MISSING, "i3"),
        (Code.EVENT_CALL_MISSING, "i3"),
    }
    uncalibrated = run(
        operation=EnumAcceptanceOperation.EVENTS,
        items=tuple(items),
        primary_verdicts=tuple(verdicts),
        agreement=_AGREEMENT.model_copy(update={"double_judged": 0}),
        event_calls=tuple(calls),
        event_judge=_JUDGE,
    )
    assert [i.code for i in uncalibrated.issues] == [Code.CALIBRATION_MISSING]
    assert uncalibrated.judged_events == ()


def test_events_deterministic() -> None:
    first = _events(*_three())
    second = _events(*_three())
    assert first.judged_events
    assert first.model_dump_json() == second.model_dump_json()


def test_events_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the events operation performed I/O")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr("builtins.open", refuse)
    result = _events(*_three())
    assert len(result.judged_events) == 3
