# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provider-to-engine table of the code try fold (OMN-24171 plan 5.1).

A mapped provider names the item's engine; a provider outside the table is refused as
``unmapped_provider``: nothing is stored, and the row is never stored as a local engine.
"""

from __future__ import annotations

import json
from pathlib import Path
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
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"


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


# --- judged event: a malformed judge_run_id is refused, never raised (OMN-26206) ---------------

#: The judged event as the producer serialises it (``model_dump(mode="json")`` of
#: ``ModelDelegationAcceptanceJudgedEvent``, the call the publish effect makes). Content-free: ids,
#: tier, verdict and rubric names only, so nothing in it needs scrubbing.
JUDGED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "judged_acceptance_live_event.json"
)


def _judged_request(payload: dict[str, Any]) -> ModelEvalProjectionRequest:
    return ModelEvalProjectionRequest(
        topic=TOPIC_JUDGED,
        payload=payload,
        tenant_id=TENANT,
        observed_at=at(40),
        lane="dev",
        host="h201",
        install_revision="rev-1",
    )


def test_judged_event_as_the_producer_emits_it_maps_to_a_reviewer_model_item() -> None:
    payload = json.loads(JUDGED_FIXTURE.read_text())
    item, refusal = judged_item(_judged_request(payload))
    assert refusal is None
    assert item is not None
    assert item.scorer is EnumEvalScorer.REVIEWER_MODEL
    assert item.engine is EnumEvalEngine.LOCAL


_MISSING = object()


def _malformed(field: str, value: object, *, with_event_id: bool) -> dict[str, Any]:
    """The fixture with ``field`` missing (``_MISSING``) or set to ``value``.

    A payload with no ``event_id`` is the shape that reached the dev writer as ``KeyError``: the
    model derives ``event_id`` in a ``default_factory`` that reads ``judge_run_id`` and
    ``correlation_id`` from the fields validated so far.
    """
    payload = json.loads(JUDGED_FIXTURE.read_text())
    if not with_event_id:
        del payload["event_id"]
    if value is _MISSING:
        del payload[field]
    else:
        payload[field] = value
    return payload


def test_judged_event_without_event_id_maps_the_same_item_as_the_producer_shape() -> (
    None
):
    with_id, _ = judged_item(_judged_request(json.loads(JUDGED_FIXTURE.read_text())))
    payload = json.loads(JUDGED_FIXTURE.read_text())
    del payload["event_id"]
    without_id, refusal = judged_item(_judged_request(payload))
    assert refusal is None
    assert without_id == with_id


@pytest.mark.parametrize("with_event_id", [True, False])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("judge_run_id", _MISSING),
        ("judge_run_id", ""),
        ("judge_run_id", "   "),
        ("judge_run_id", None),
        ("judge_run_id", 7),
        ("judge_run_id", ["judge-run"]),
        ("correlation_id", _MISSING),
        ("correlation_id", "not-a-uuid"),
        ("correlation_id", 7),
    ],
)
def test_judged_event_with_a_malformed_id_field_is_refused_with_a_typed_reason(
    field: str, value: object, with_event_id: bool
) -> None:
    payload = _malformed(field, value, with_event_id=with_event_id)
    item, refusal = judged_item(_judged_request(payload))
    assert item is None
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT
    assert refusal.subject == "judged"
    assert field in refusal.detail


def test_judged_event_with_a_valid_judge_run_id_and_another_invalid_field_names_that_field() -> (
    None
):
    payload = json.loads(JUDGED_FIXTURE.read_text())
    payload["quality"] = 9
    _, refusal = judged_item(_judged_request(payload))
    assert refusal is not None
    assert refusal.reason is EnumEvalRefusal.INVALID_EVENT
    assert "quality" in refusal.detail
    assert "judge_run_id" not in refusal.detail
