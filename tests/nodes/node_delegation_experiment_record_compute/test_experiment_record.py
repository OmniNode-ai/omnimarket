# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The experiment results record check enforces the four acceptance criteria of the program."""

from __future__ import annotations

import pytest

from omnimarket.models.delegation_experiment_record.enum_experiment_refusal import (
    EnumExperimentRefusal,
)
from omnimarket.models.delegation_experiment_record.enum_experiment_verdict import (
    EnumExperimentVerdict,
)
from omnimarket.models.delegation_experiment_record.model_experiment_arm_result import (
    ModelExperimentArmResult,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record import (
    ModelExperimentRecord,
)
from omnimarket.models.delegation_experiment_record.model_experiment_record_request import (
    ModelExperimentRecordRequest,
)
from omnimarket.nodes.node_delegation_experiment_record_compute.handlers.handler_delegation_experiment_record import (
    HandlerDelegationExperimentRecord,
)

pytestmark = pytest.mark.unit

RUNS_A = ("run-a1", "run-a2", "run-a3")
RUNS_B = ("run-b1", "run-b2", "run-b3")


def _arm(
    arm_id: str = "arm-a",
    model: str = "Qwen3.8-27B",
    value: float | None = 0.5,
    run_ids: tuple[str, ...] = RUNS_A,
) -> ModelExperimentArmResult:
    return ModelExperimentArmResult(
        arm_id=arm_id,
        model=model,
        run_ids=run_ids,
        per_arm_value=value,
    )


def _record(**overrides: object) -> ModelExperimentRecord:
    fields: dict[str, object] = {
        "experiment_id": "exp-1",
        "hypothesis": "A structured brief raises judged acceptance.",
        "metric": "judged acceptance rate",
        "baseline": 0.5,
        "arms": (_arm(), _arm("arm-b", value=0.7, run_ids=RUNS_B)),
        "verdict": EnumExperimentVerdict.REFUTED,
    }
    fields.update(overrides)
    return ModelExperimentRecord.model_validate(fields)


def _request(
    *records: ModelExperimentRecord,
    listed: tuple[str, ...] | None = None,
) -> ModelExperimentRecordRequest:
    return ModelExperimentRecordRequest(
        listed_experiment_ids=(
            listed if listed is not None else tuple(r.experiment_id for r in records)
        ),
        records=records,
        known_receipt_ids=RUNS_A + RUNS_B,
    )


def _refusals(*records: ModelExperimentRecord, **kw: object) -> set[str]:
    listed = kw.get("listed")
    assert listed is None or isinstance(listed, tuple)
    result = HandlerDelegationExperimentRecord().handle(
        _request(*records, listed=listed)
    )
    return {issue.refusal.value for issue in result.issues}


def test_complete_record_is_accepted_with_run_count_derived() -> None:
    result = HandlerDelegationExperimentRecord().handle(_request(_record()))
    assert result.issues == ()
    assert result.accepted_experiment_ids == ("exp-1",)
    assert [(c.experiment_id, c.run_count) for c in result.run_counts] == [("exp-1", 6)]


def test_listed_experiment_with_no_record_is_refused() -> None:
    assert _refusals(_record(), listed=("exp-1", "exp-2")) == {
        EnumExperimentRefusal.MISSING_RECORD.value
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"hypothesis": ""}, EnumExperimentRefusal.MISSING_HYPOTHESIS),
        ({"metric": " "}, EnumExperimentRefusal.MISSING_METRIC),
        ({"baseline": None}, EnumExperimentRefusal.MISSING_BASELINE),
        ({"verdict": None}, EnumExperimentRefusal.MISSING_VERDICT),
        ({"arms": (_arm(),)}, EnumExperimentRefusal.TOO_FEW_ARMS),
        (
            {"arms": (_arm(value=None), _arm("arm-b", run_ids=RUNS_B))},
            EnumExperimentRefusal.MISSING_ARM_VALUE,
        ),
        (
            {"arms": (_arm(run_ids=()), _arm("arm-b", run_ids=RUNS_B))},
            EnumExperimentRefusal.EMPTY_ARM,
        ),
        (
            {"arms": (_arm(), _arm("arm-a", run_ids=RUNS_B))},
            EnumExperimentRefusal.DUPLICATE_ARM,
        ),
        (
            {"arms": (_arm(), _arm("arm-b", run_ids=RUNS_A))},
            EnumExperimentRefusal.DUPLICATE_RUN,
        ),
    ],
)
def test_ac1_record_missing_a_field_is_refused(
    overrides: dict[str, object], expected: EnumExperimentRefusal
) -> None:
    assert expected.value in _refusals(_record(**overrides))


def test_void_verdict_needs_a_cause() -> None:
    assert EnumExperimentRefusal.VOID_WITHOUT_CAUSE.value in _refusals(
        _record(verdict=EnumExperimentVerdict.VOID)
    )
    assert (
        _refusals(
            _record(verdict=EnumExperimentVerdict.VOID, void_cause="server restarted")
        )
        == set()
    )


def test_ac2_run_id_without_a_receipt_is_refused_and_named() -> None:
    record = _record(
        arms=(_arm(run_ids=("run-a1", "run-ghost")), _arm("arm-b", run_ids=RUNS_B))
    )
    result = HandlerDelegationExperimentRecord().handle(_request(record))
    ghosts = [
        i for i in result.issues if i.refusal is EnumExperimentRefusal.UNTRACEABLE_RUN
    ]
    assert [(i.experiment_id, i.arm_id, i.detail) for i in ghosts] == [
        ("exp-1", "arm-a", "run-ghost")
    ]
    assert result.accepted_experiment_ids == ()


def test_ac2_zero_known_receipts_refuses_every_run() -> None:
    request = ModelExperimentRecordRequest(
        listed_experiment_ids=("exp-1",), records=(_record(),), known_receipt_ids=()
    )
    result = HandlerDelegationExperimentRecord().handle(request)
    assert {i.refusal for i in result.issues} == {EnumExperimentRefusal.UNTRACEABLE_RUN}
    assert len(result.issues) == 6


@pytest.mark.parametrize(
    "path",
    [
        "src/omnimarket/configs/bifrost_delegation.yaml",
        "delegation/overlays/bifrost_overrides.dev.yaml",
    ],
)
def test_ac3_change_to_routing_path_is_refused(path: str) -> None:
    assert EnumExperimentRefusal.ROUTING_CHANGE.value in _refusals(
        _record(changed_paths=(path,))
    )


def test_ac3_change_to_a_results_path_is_accepted() -> None:
    assert _refusals(_record(changed_paths=("results/exp-1.json",))) == set()


def test_ac4_supported_verdict_needs_follow_on_or_ruling() -> None:
    supported = {"verdict": EnumExperimentVerdict.SUPPORTED}
    assert EnumExperimentRefusal.SUPPORTED_WITHOUT_FOLLOW_ON.value in _refusals(
        _record(**supported)
    )
    assert _refusals(_record(**supported, follow_on_refs=("OMN-1",))) == set()
    assert _refusals(_record(**supported, declining_ruling_ref="ledger:1")) == set()


def test_retired_model_arm_is_refused() -> None:
    record = _record(arms=(_arm(model="gpt-oss-120b"), _arm("arm-b", run_ids=RUNS_B)))
    assert EnumExperimentRefusal.RETIRED_MODEL.value in _refusals(record)


def test_duplicate_experiment_id_is_refused() -> None:
    assert EnumExperimentRefusal.DUPLICATE_EXPERIMENT.value in _refusals(
        _record(), _record()
    )


def test_one_bad_record_does_not_hide_a_good_one() -> None:
    good = _record()
    bad = _record(experiment_id="exp-2", metric="")
    result = HandlerDelegationExperimentRecord().handle(_request(good, bad))
    assert result.accepted_experiment_ids == ("exp-1",)
    assert {i.experiment_id for i in result.issues} == {"exp-2"}
