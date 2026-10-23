# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20168: every attempt event carries its size band and configuration.

A real escalating local dispatch stamps each rung with the request's measured
size band and the call parameters it was sent with. Split children and
recombine checks keep their producer lineage and gain the same fields. A rung
redelivered under an attempt id already in the ladder is kept once, and the
same backend and model retried with another configuration is a config_swap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind
from omnimarket.enums.enum_delegation_context_strategy import (
    EnumDelegationContextStrategy,
)
from omnimarket.models.delegation.delegation_attempt_lineage import (
    attempt_id_for,
    stamp_attempt_lineage,
)
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from tests.test_omn20168_local_dispatch_attempt_lineage_seam import (
    _backend,
    _install_ladder,
    _run,
)
from tests.unit.delegation.test_attempt_events_omn20168 import _ladder, _project

pytestmark = pytest.mark.unit

_CONFIGURATION = {
    "max_tokens": 256,
    "temperature": None,
    "top_p": None,
    "reasoning_effort": None,
    "context_strategy_id": "full_context",
}
_SWAPPED = _CONFIGURATION | {"temperature": 0.2, "context_strategy_id": "task_restated"}


def _assert_event_fields(rung: dict[str, Any], *, band: str) -> None:
    assert rung["backend_id"]
    assert rung["model_id"]
    assert rung["host"]
    assert rung["size_band"] == band
    assert rung["rubric_verdict"]["rubric_version"]
    assert rung["rubric_verdict"]["outcome"] in {"PASS", "FAIL", "UNDETERMINED"}
    assert rung["attempt_kind"] in {kind.value for kind in EnumDelegationAttemptKind}
    assert UUID(rung["attempt_id"])
    assert "parent_attempt_id" in rung


def _split_ladder(correlation_id: UUID) -> list[dict[str, object]]:
    """A first try, a split child of it, and the recombine check after the child."""
    first, child, recombine = _ladder(3)
    split_id = str(uuid4())
    first_id = str(attempt_id_for(correlation_id, 0))
    child_id = str(attempt_id_for(correlation_id, 1))
    child.update(
        attempt_kind="split_child", parent_attempt_id=first_id, split_id=split_id
    )
    recombine.update(
        attempt_kind="recombine_check", parent_attempt_id=child_id, split_id=split_id
    )
    return [first, child, recombine]


def test_attempt_event_fields_on_a_first_try_and_an_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    correlation_id, result, stored = _run(tmp_path)

    assert [rung["attempt_kind"] for rung in stored] == [
        "first_try",
        "whole_escalation",
    ]
    for rung in stored:
        _assert_event_fields(rung, band="small")
    assert stored[1]["parent_attempt_id"] == stored[0]["attempt_id"]
    records = _attempt_records(result | {"correlation_id": str(correlation_id)})
    assert [record.size_band for record in records] == ["small", "small"]
    assert [record.attempt_id for record in records] == [
        UUID(rung["attempt_id"]) for rung in stored
    ]


def test_attempt_event_fields_on_a_split_child_and_a_recombine_check(
    tmp_path: Path,
) -> None:
    correlation_id = uuid4()
    rungs = stamp_attempt_lineage(
        _split_ladder(correlation_id),
        correlation_id=correlation_id,
        size_band="medium",
        configuration=_CONFIGURATION,
    )
    stored = _project(tmp_path, rungs, correlation_id)

    assert [rung["attempt_kind"] for rung in stored] == [
        "first_try",
        "split_child",
        "recombine_check",
    ]
    for rung in stored:
        _assert_event_fields(rung, band="medium")
        assert rung["configuration"] == _CONFIGURATION
    assert stored[1]["parent_attempt_id"] == stored[0]["attempt_id"]
    assert stored[2]["parent_attempt_id"] == stored[1]["attempt_id"]
    assert stored[1]["split_id"] == stored[2]["split_id"] is not None
    records = [ModelDelegateSkillAttemptRecord.model_validate(rung) for rung in stored]
    assert [record.attempt_kind for record in records] == [
        EnumDelegationAttemptKind.FIRST_TRY,
        EnumDelegationAttemptKind.SPLIT_CHILD,
        EnumDelegationAttemptKind.RECOMBINE_CHECK,
    ]


def test_attempt_event_idempotent_when_a_rung_is_redelivered(tmp_path: Path) -> None:
    correlation_id = uuid4()
    ladder = _ladder(2)
    for index, rung in enumerate(ladder):
        rung["attempt_id"] = str(attempt_id_for(correlation_id, index))
    redelivered = [*ladder, dict(ladder[1])]

    stamped = stamp_attempt_lineage(redelivered, correlation_id=correlation_id)
    assert [rung["attempt_id"] for rung in stamped] == [
        rung["attempt_id"] for rung in ladder
    ]
    stored = _project(tmp_path, redelivered, correlation_id)
    assert len(stored) == 2
    assert len({rung["attempt_id"] for rung in stored}) == 2
    records = _attempt_records(
        {"correlation_id": str(correlation_id), "attempts": redelivered}
    )
    assert len(records) == 2

    # The whole terminal delivered again is the same row, not a second one.
    again = _project(tmp_path, redelivered, correlation_id)
    assert again == stored


def test_attempt_event_configuration_on_every_rung_of_a_real_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    correlation_id, result, stored = _run(tmp_path)

    assert [rung["configuration"] for rung in stored] == [_CONFIGURATION] * 2
    records = _attempt_records(result | {"correlation_id": str(correlation_id)})
    assert [record.configuration for record in records] == [
        ModelAttemptConfiguration(
            max_tokens=256,
            context_strategy_id=EnumDelegationContextStrategy.FULL_CONTEXT,
        )
    ] * 2


def test_config_swap_attempt_kind_for_the_same_backend_with_another_configuration() -> (
    None
):
    correlation_id = uuid4()
    first, retry = _ladder(2)
    retry.update(backend_id=first["backend_id"], model_id=first["model_id"])
    first["configuration"] = _CONFIGURATION
    retry["configuration"] = _SWAPPED

    stamped = stamp_attempt_lineage([first, retry], correlation_id=correlation_id)

    assert stamped[1]["attempt_kind"] == "config_swap"
    assert stamped[1]["parent_attempt_id"] == stamped[0]["attempt_id"]
    record = ModelDelegateSkillAttemptRecord.model_validate(stamped[1])
    assert record.attempt_kind is EnumDelegationAttemptKind.CONFIG_SWAP
    assert record.configuration == ModelAttemptConfiguration.model_validate(_SWAPPED)


@pytest.mark.parametrize(
    "change",
    [
        {"backend_id": "another-backend"},
        {"configuration": _CONFIGURATION},
    ],
    ids=["another_backend", "same_configuration"],
)
def test_config_swap_attempt_kind_only_when_backend_holds_and_configuration_moves(
    change: dict[str, object],
) -> None:
    first, retry = _ladder(2)
    retry.update(backend_id=first["backend_id"], model_id=first["model_id"])
    first["configuration"] = _CONFIGURATION
    retry["configuration"] = _SWAPPED
    retry.update(change)

    stamped = stamp_attempt_lineage([first, retry], correlation_id=uuid4())

    assert stamped[1]["attempt_kind"] == "whole_escalation"


def test_an_escalated_dispatch_writes_one_event_per_attempt_with_distinct_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch)
    _, result, stored = _run(tmp_path / "escalated")

    assert len(stored) == len(result["attempts"]) == 2
    assert len({rung["attempt_id"] for rung in stored}) == 2
    assert len({rung["backend_id"] for rung in stored}) == 2
    assert all(rung["configuration"] == _CONFIGURATION for rung in stored)

    # Positive control: a run the first rung answers writes exactly one event.
    monkeypatch.setattr(
        port_mod, "first_eligible_tier", lambda *_a, **_k: "cheap_cloud"
    )
    monkeypatch.setattr(
        port_mod,
        "resolve_delegation_backend",
        lambda *_a, **_k: _backend("cheap_cloud"),
    )
    _, single_result, single = _run(tmp_path / "single")
    assert len(single) == len(single_result["attempts"]) == 1
    assert single[0]["attempt_kind"] == "first_try"
    assert single[0]["configuration"] == _CONFIGURATION
