# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20012: a disposition published through the emitter reaches the fold.

The producers (the delegation receipt tool and the onex delegate callers)
publish ``ModelDelegationDispositionRecorded`` through
``node_event_emit_effect``. Its publish-time enrichment adds envelope keys to
every payload, and the projection request is ``extra="forbid"``, so before
this change every published disposition was refused at the projection and
never written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_disposition import (
    EnumDelegationArtifactKind,
    EnumDelegationDisposition,
    EnumDelegationDispositionReason,
)
from omnimarket.events.delegation_disposition import ModelDelegationDispositionRecorded
from omnimarket.nodes.node_event_emit_effect.enrichment import (
    ENRICHMENT_FIELDS,
    inject_metadata,
)
from omnimarket.nodes.node_projection_delegation_disposition.handlers.handler_projection_delegation_disposition import (
    HandlerProjectionDelegationDisposition,
)
from omnimarket.nodes.node_projection_delegation_disposition.models import (
    ModelDelegationDispositionProjectionRequest,
)
from omnimarket.nodes.node_projection_delegation_disposition.models.model_delegation_disposition import (
    EMIT_ENRICHMENT_KEYS,
)

pytestmark = pytest.mark.unit


def _event() -> ModelDelegationDispositionRecorded:
    return ModelDelegationDispositionRecorded.build(
        tenant_id=uuid4(),
        delegation_correlation_id=uuid4(),
        disposition=EnumDelegationDisposition.EDITED,
        reason_code=EnumDelegationDispositionReason.MINOR_FIX,
        caller_lane="disposition-and-248",
        engine="lab",
        artifact_kind=EnumDelegationArtifactKind.PULL_REQUEST,
        artifact_ref="OmniNode-ai/omnimarket#1",
        edit_ratio=0.25,
        recorded_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )


def _enriched(event: ModelDelegationDispositionRecorded) -> dict[str, object]:
    return inject_metadata(
        event.model_dump(mode="json"),
        env={"CLAUDE_CODE_SESSION_ID": "session-1"},
    )


def test_strip_set_is_the_emitters_enrichment_set() -> None:
    assert frozenset(ENRICHMENT_FIELDS) == EMIT_ENRICHMENT_KEYS


def test_enriched_payload_is_accepted_and_keeps_its_identity() -> None:
    event = _event()
    payload = _enriched(event)
    assert set(payload) - set(event.model_dump()) == set(ENRICHMENT_FIELDS)

    request = ModelDelegationDispositionProjectionRequest.model_validate(payload)

    assert request.disposition_id == event.disposition_id
    assert request.delegation_correlation_id == event.delegation_correlation_id
    assert request.edit_ratio == 0.25


def test_enriched_payload_folds_to_exactly_one_row() -> None:
    event = _event()
    request = ModelDelegationDispositionProjectionRequest.model_validate(
        _enriched(event)
    )

    result = HandlerProjectionDelegationDisposition().handle(request)

    assert len(result.rows) == 1
    assert result.rows[0].disposition_id == event.disposition_id


@pytest.mark.parametrize("field", ["answer_text", "prompt", "response"])
def test_a_field_the_producer_put_on_the_wire_is_still_refused(field: str) -> None:
    payload = _enriched(_event())
    payload[field] = "content never belongs on this event"
    with pytest.raises(ValidationError):
        ModelDelegationDispositionProjectionRequest.model_validate(payload)
