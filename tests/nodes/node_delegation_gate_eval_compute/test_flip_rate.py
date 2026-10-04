# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generation stability counts disagreement once per three-generation group."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.models.delegation_gate_eval.model_generation_stability_group import (
    ModelGenerationStabilityGroup,
)
from omnimarket.models.delegation_gate_eval.model_generation_stability_request import (
    ModelGenerationStabilityRequest,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
    compute_generation_stability,
    wilson_interval,
)

pytestmark = pytest.mark.unit

_ACCEPTED = EnumGateVerdict.ACCEPTED
_REFUSED = EnumGateVerdict.REFUSED
_UNDETERMINED = EnumGateVerdict.UNDETERMINED


def _group(
    item_id: str,
    verdicts: tuple[EnumGateVerdict, EnumGateVerdict, EnumGateVerdict],
    task_class: str = "summarization",
    backend_id: str = "local-test-backend",
) -> ModelGenerationStabilityGroup:
    return ModelGenerationStabilityGroup(
        task_class=task_class,
        item_id=item_id,
        backend_id=backend_id,
        verdicts=verdicts,
    )


@pytest.mark.parametrize(
    ("verdicts", "flips"),
    [
        ((_ACCEPTED, _ACCEPTED, _REFUSED), 1),
        ((_REFUSED, _ACCEPTED, _ACCEPTED), 1),
        ((_ACCEPTED, _REFUSED, _ACCEPTED), 1),
        ((_ACCEPTED, _ACCEPTED, _ACCEPTED), 0),
        ((_UNDETERMINED, _UNDETERMINED, _UNDETERMINED), 0),
        ((_ACCEPTED, _REFUSED, _UNDETERMINED), 1),
    ],
)
def test_flip_rate_counts_a_group_once(
    verdicts: tuple[EnumGateVerdict, EnumGateVerdict, EnumGateVerdict], flips: int
) -> None:
    result = compute_generation_stability(
        ModelGenerationStabilityRequest(groups=(_group("one", verdicts),))
    )
    (row,) = result.rows
    assert row.flips_n == flips
    assert row.groups_n == 1
    assert row.flip_rate == flips


def test_flip_rate_per_class_uses_groups_and_wilson_bound() -> None:
    request = ModelGenerationStabilityRequest(
        groups=(
            _group("one", (_ACCEPTED, _REFUSED, _UNDETERMINED)),
            _group("two", (_ACCEPTED, _ACCEPTED, _ACCEPTED)),
            _group("three", (_REFUSED, _REFUSED, _REFUSED)),
            _group("one", (_REFUSED, _REFUSED, _REFUSED), task_class="test"),
        )
    )
    result = compute_generation_stability(request)
    summary, test = result.rows
    assert summary.task_class == "summarization"
    assert (summary.flips_n, summary.groups_n) == (1, 3)
    assert summary.flip_rate == pytest.approx(1 / 3)
    assert summary.flip_rate_wilson == wilson_interval(1, 3)
    assert summary.measured_backend_id == "local-test-backend"
    assert test.task_class == "test"
    assert (test.flips_n, test.groups_n, test.flip_rate) == (0, 1, 0.0)
    assert test.flip_rate_wilson == wilson_interval(0, 1)
    assert (
        compute_generation_stability(
            ModelGenerationStabilityRequest(groups=tuple(reversed(request.groups)))
        )
        == result
    )


def test_flip_rate_empty_class_yields_no_row() -> None:
    assert compute_generation_stability(ModelGenerationStabilityRequest()).rows == ()


def test_flip_rate_fields_on_gate_rate_rows() -> None:
    item = ModelGateEvalItem(
        item_id="one",
        task_class="summarization",
        stratum="default",
        label=EnumGateEvalLabel.ADEQUATE,
        prompt_text="Summarize the release.",
        recorded_verdict=_ACCEPTED,
    )
    unmeasured_item = item.model_copy(update={"task_class": "test"})
    request = ModelDelegationGateEvalRequest(
        run_id="test", items=(item, unmeasured_item)
    )
    handler = HandlerDelegationGateEval()
    original = handler.handle(request)
    assert original.rate_rows
    for row in original.rate_rows:
        assert row.flip_rate is None
        assert row.flip_rate_wilson is None
        assert row.measured_backend_id is None

    result = handler.handle(
        request.model_copy(
            update={
                "generation_stability": ModelGenerationStabilityRequest(
                    groups=(_group("one", (_ACCEPTED, _ACCEPTED, _REFUSED)),)
                )
            }
        )
    )
    for row, baseline in zip(result.rate_rows, original.rate_rows, strict=True):
        if row.task_class == "summarization":
            assert row.flip_rate == 1.0
            assert row.flip_rate_wilson == wilson_interval(1, 1)
            assert row.measured_backend_id == "local-test-backend"
        else:
            assert row.flip_rate is None
            assert row.flip_rate_wilson is None
            assert row.measured_backend_id is None
        assert row.model_dump(
            exclude={"flip_rate", "flip_rate_wilson", "measured_backend_id"}
        ) == baseline.model_dump(
            exclude={"flip_rate", "flip_rate_wilson", "measured_backend_id"}
        )
    assert result.status == original.status
    assert result.nondeterministic_items == ()


@pytest.mark.parametrize(
    "verdicts", [(), (_ACCEPTED,), (_ACCEPTED,) * 2, (_ACCEPTED,) * 4]
)
def test_flip_rate_requires_exactly_three_verdicts(
    verdicts: tuple[EnumGateVerdict, ...],
) -> None:
    with pytest.raises(ValidationError):
        ModelGenerationStabilityGroup(
            task_class="test", item_id="one", backend_id="local", verdicts=verdicts
        )


def test_flip_rate_rejects_mixed_backends_and_duplicate_groups() -> None:
    first = _group("one", (_ACCEPTED, _ACCEPTED, _ACCEPTED))
    with pytest.raises(ValidationError, match="one backend"):
        ModelGenerationStabilityRequest(
            groups=(first, _group("two", first.verdicts, backend_id="other"))
        )
    with pytest.raises(ValidationError, match="unique"):
        ModelGenerationStabilityRequest(groups=(first, first))
