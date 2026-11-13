# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam: producer intent-classified wire dump -> both consumers.

The intent-classified producer (``node_prompt_intent_classify_compute``) must
only ever emit events its consumers accept. This test builds the producer's
canonical model for every value of ``EnumIntentClass`` plus the ``unknown``
fallback, dumps it to JSON exactly as the producer does (``model_dump_json``
then ``json.loads``), and asserts BOTH consumer models validate it:

* ``omnimemory.models.events.ModelIntentClassifiedEvent`` (the
  ``node_intent_event_consumer_effect`` input; needs a UUID ``correlation_id``
  and a required ``datetime`` ``emitted_at``), and
* ``omnimarket.nodes.node_projection_intent_classification``'s
  ``ModelIntentClassifiedEvent`` (string ``correlation_id``, extra ignored).

It also asserts the producer model itself refuses an ``emitted_at`` of
``None`` and a non-UUID ``correlation_id`` (the failing control: on the
unchanged code those two assertions fail).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from omnibase_core.enums.intelligence.enum_intent_class import EnumIntentClass
from omnimemory.models.events import (
    ModelIntentClassifiedEvent as MemoryModelIntentClassifiedEvent,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_intent_classification.handlers.handler_projection_intent_classification import (
    ModelIntentClassifiedEvent as ProjectionModelIntentClassifiedEvent,
)
from omnimarket.nodes.node_prompt_intent_classify_compute.models.model_intent_classified import (
    ModelIntentClassified,
)

_CORRELATION_ID = "11111111-2222-3333-4444-555555555555"
_EMITTED_AT = "2026-09-25T11:40:00+00:00"


def _producer_event(intent_class: str) -> ModelIntentClassified:
    return ModelIntentClassified(
        session_id="sess-1",
        correlation_id=_CORRELATION_ID,
        intent_class=intent_class,
        intent_category="debugging",
        confidence=0.9,
        keywords=["fix"],
        emitted_at=_EMITTED_AT,
        agent_source="claude",
    )


def _to_wire(event: ModelIntentClassified) -> dict[str, Any]:
    """Serialize via the producer's model, then decode as the consumer does."""
    return json.loads(event.model_dump_json())


@pytest.mark.unit
@pytest.mark.parametrize(
    "intent_class",
    [member.value for member in EnumIntentClass] + ["unknown"],
)
def test_producer_event_validates_in_both_consumers(intent_class: str) -> None:
    wire = _to_wire(_producer_event(intent_class))

    memory_event = MemoryModelIntentClassifiedEvent.model_validate(wire)
    projection_event = ProjectionModelIntentClassifiedEvent.model_validate(wire)

    assert memory_event.correlation_id is not None
    assert memory_event.emitted_at is not None
    assert projection_event.correlation_id == _CORRELATION_ID
    # Both consumers keep a recognised class as is and map an unrecognised one
    # to the analysis fallback instead of refusing the event.
    expected = intent_class if intent_class != "unknown" else "analysis"
    assert projection_event.intent_class == expected
    assert memory_event.intent_class in set(EnumIntentClass)


@pytest.mark.unit
def test_producer_model_refuses_missing_emitted_at() -> None:
    with pytest.raises(ValidationError):
        ModelIntentClassified(
            session_id="sess-1",
            correlation_id=_CORRELATION_ID,
            intent_class="BUGFIX",
            intent_category="debugging",
            confidence=0.9,
            keywords=["fix"],
            emitted_at=None,
            agent_source="claude",
        )


@pytest.mark.unit
def test_producer_model_refuses_non_uuid_correlation_id() -> None:
    with pytest.raises(ValidationError):
        ModelIntentClassified(
            session_id="sess-1",
            correlation_id="corr-1",
            intent_class="BUGFIX",
            intent_category="debugging",
            confidence=0.9,
            keywords=["fix"],
            emitted_at=_EMITTED_AT,
            agent_source="claude",
        )
