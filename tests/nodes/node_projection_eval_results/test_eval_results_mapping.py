# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provider-to-engine table of the code try fold (OMN-24171 plan 5.1).

A mapped provider names the item's engine; a provider outside the table is refused as
``unmapped_provider``: nothing is stored, and the row is never stored as a local engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_mapping import (
    code_try_items,
    judged_item,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    ModelEvalProjectionRequest,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    at,
    code_event,
    judged_event,
    recorded_judged_render_result,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
CALL = "00000000-0000-0000-0000-00000000c001"


def _request(payload: dict[str, Any]) -> ModelEvalProjectionRequest:
    return ModelEvalProjectionRequest(
        topic=TOPIC_CODE_TRY,
        payload=payload,
        tenant_id=TENANT,
        observed_at=at(40),
        lane="dev",
        host="h202",
        install_revision="rev-1",
    )


@pytest.mark.parametrize(
    ("provider", "engine"),
    [
        ("local", EnumEvalEngine.LOCAL),
        ("glm", EnumEvalEngine.GLM),
        ("codex", EnumEvalEngine.CODEX),
        ("claude", EnumEvalEngine.CLAUDE),
        ("anthropic", EnumEvalEngine.CLAUDE),
    ],
)
def test_eval_results_mapping_a_mapped_provider_names_its_engine(
    provider: str, engine: EnumEvalEngine
) -> None:
    items, refused = code_try_items(
        _request(code_event(RUN, try_row(RUN, "t1", provider=provider)))
    )
    assert refused == ()
    (item,) = items
    assert item.engine == engine
    assert item.task_id == "t1"


@pytest.mark.parametrize("provider", ["gemini", "openrouter", ""])
def test_eval_results_mapping_an_unmapped_provider_is_refused(provider: str) -> None:
    items, refused = code_try_items(
        _request(code_event(RUN, try_row(RUN, "t1", provider=provider)))
    )
    assert items == ()
    (part,) = refused
    assert part.reason is EnumEvalRefusal.UNMAPPED_PROVIDER
    assert part.detail == provider


def test_eval_results_mapping_a_mixed_event_keeps_only_its_mapped_row() -> None:
    """A local row and a gemini row in one event: only the mapped row is stored."""
    items, refused = code_try_items(
        _request(
            code_event(RUN, try_row(RUN, "t1"), try_row(RUN, "t2", provider="gemini"))
        )
    )
    assert len(items) == 1
    item = items[0]
    assert item.engine == EnumEvalEngine.LOCAL
    assert item.task_id == "t1"
    assert len(refused) == 1
    part = refused[0]
    assert part.reason is EnumEvalRefusal.UNMAPPED_PROVIDER
    assert part.subject.startswith(f"{RUN}:t2:")
    assert part.detail == "gemini"


def _judged_request(payload: dict[str, Any]) -> ModelEvalProjectionRequest:
    return ModelEvalProjectionRequest(
        topic=TOPIC_JUDGED,
        payload=payload,
        tenant_id=TENANT,
        observed_at=at(40),
        lane="dev",
        host="h202",
        install_revision="rev-1",
    )


def test_judged_mapping_the_recorded_dev_broker_event_is_refused_typed_not_raised() -> (
    None
):
    """The payload on the dev broker's judged topic is the judge's render result, with no judge_run_id."""
    recorded = recorded_judged_render_result()
    assert "judge_run_id" not in recorded
    item, refusal = judged_item(_judged_request(recorded))
    assert item is None
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT
    assert "judge_run_id" in refusal.detail


def _judged_payload(*, drop: tuple[str, ...] = (), event_id: bool) -> dict[str, Any]:
    """A valid judged payload less ``drop``; without ``event_id`` the model derives it from the ids."""
    skip = set(drop) | (set() if event_id else {"event_id"})
    return {k: v for k, v in judged_event(CALL).items() if k not in skip}


@pytest.mark.parametrize("with_event_id", [True, False])
@pytest.mark.parametrize("bad", ["", "   ", None, 7, ["judge-1"], {"id": "judge-1"}])
def test_judged_mapping_a_bad_judge_run_id_is_refused_naming_the_field(
    bad: Any, with_event_id: bool
) -> None:
    payload = {**_judged_payload(event_id=with_event_id), "judge_run_id": bad}
    item, refusal = judged_item(_judged_request(payload))
    assert item is None
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT
    assert "judge_run_id" in refusal.detail


@pytest.mark.parametrize("with_event_id", [True, False])
@pytest.mark.parametrize("field", ["judge_run_id", "correlation_id"])
def test_judged_mapping_a_missing_event_id_input_is_refused_naming_the_field(
    field: str, with_event_id: bool
) -> None:
    payload = _judged_payload(drop=(field,), event_id=with_event_id)
    item, refusal = judged_item(_judged_request(payload))
    assert item is None
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT
    assert field in refusal.detail


def test_judged_mapping_a_bad_field_other_than_the_id_inputs_is_still_a_typed_refusal() -> (
    None
):
    payload = {**judged_event(CALL), "accept": "maybe"}
    item, refusal = judged_item(_judged_request(payload))
    assert item is None
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT


def test_judged_mapping_a_valid_event_still_maps_to_its_reviewer_model_item() -> None:
    item, refusal = judged_item(_judged_request(judged_event(CALL, accept=False)))
    assert refusal is None
    assert item is not None
    assert item.scorer is EnumEvalScorer.REVIEWER_MODEL
    assert item.engine is EnumEvalEngine.LOCAL
    assert item.task_id == CALL
    assert item.passed is False
    assert item.run_id.startswith("live-")
    assert item.tenant_id == TENANT


@pytest.mark.parametrize("rows", [5, None, True, 1.5])
def test_eval_results_mapping_a_code_event_whose_rows_are_not_a_list_is_refused(
    rows: Any,
) -> None:
    items, refused = code_try_items(_request({"run_id": RUN, "rows": rows}))
    assert items == ()
    (part,) = refused
    assert part.reason is EnumEvalRefusal.INVALID_EVENT
    assert "rows" in part.detail
