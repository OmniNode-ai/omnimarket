# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478: the install-confirm trigger accepts the applied event the runtime publishes.

The dev runtime published every ``projection-session-content-applied`` event with
the projection handler's result plus the acknowledgement key ``projected: true``
(OMN-16875). ``ModelSessionContentAppliedEvent`` forbids extra keys, so the
dispatcher refused each payload (``projected: Extra inputs are not permitted``) and
sent it to ``onex.dlq.omnibase-infra.events.v1`` at about 150 records a minute.

``RECORDED_PAYLOAD`` is the ``payload`` of one such record, copied byte for byte from
the dead-letter topic's ``original_message.value`` (offset 23357700,
2026-11-06T09:48:58Z). The golden
test replays it through the same ``model_validate`` the dispatcher runs and on into
the trigger handler; the error tests keep the model strict about everything else.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.context_install_confirm_trigger import (
    EnumInstallConfirmTriggerOutcome,
    ModelInstallConfirmTriggerResult,
    ModelSessionCompactSnapshot,
    ModelSessionContentAppliedEvent,
)
from omnimarket.nodes.node_context_install_confirm_trigger_effect.handlers.handler_context_install_confirm_trigger import (
    HandlerContextInstallConfirmTrigger,
)
from tests.nodes.node_context_install_confirm_request_compute.test_handler_context_install_confirm_request import (
    SESSION,
    activation,
    chunks,
    package,
)

pytestmark = pytest.mark.unit

RECORDED_PAYLOAD = (
    '{"rows_upserted":1,"event_id":'
    '"1a1feaba4b4ad77234eae5cc83c8d5336c86f6835f3cd8b40f861a0738a4b42f",'
    '"projected":true}'
)
RECORDED_EVENT_ID = "1a1feaba4b4ad77234eae5cc83c8d5336c86f6835f3cd8b40f861a0738a4b42f"


class _Reader:
    def __init__(self, snapshot: ModelSessionCompactSnapshot) -> None:
        self.snapshot = snapshot
        self.read_ids: list[str] = []

    def read(self, event_id: str) -> ModelSessionCompactSnapshot | None:
        self.read_ids.append(event_id)
        return self.snapshot


def _recorded_payload() -> dict[str, Any]:
    payload = json.loads(RECORDED_PAYLOAD)
    assert isinstance(payload, dict)
    return payload


def test_golden_the_recorded_runtime_payload_validates_and_drives_the_trigger() -> None:
    payload = _recorded_payload()
    # The runtime's own keys, exactly: the handler's two plus the acknowledgement.
    assert set(payload) == {"event_id", "rows_upserted", "projected"}

    applied = ModelSessionContentAppliedEvent.model_validate(payload)

    assert applied.event_id == RECORDED_EVENT_ID
    assert applied.rows_upserted == 1
    assert applied.projected is True

    reader = _Reader(
        ModelSessionCompactSnapshot(
            session_id=SESSION,
            chunks=chunks(package(), 3),
            activations=(activation(),),
        )
    )
    result = HandlerContextInstallConfirmTrigger(reader=reader).handle(applied)

    assert isinstance(result, ModelInstallConfirmTriggerResult)
    assert reader.read_ids == [RECORDED_EVENT_ID]
    assert result.outcome is EnumInstallConfirmTriggerOutcome.REQUEST_READY
    assert result.confirmation is not None


def test_golden_a_payload_without_the_acknowledgement_still_validates() -> None:
    payload = _recorded_payload()
    del payload["projected"]

    applied = ModelSessionContentAppliedEvent.model_validate(payload)

    assert applied.projected is True


def test_error_an_unknown_key_beside_the_acknowledgement_is_still_refused() -> None:
    payload = _recorded_payload() | {"unexpected": True}

    with pytest.raises(ValidationError) as refused:
        ModelSessionContentAppliedEvent.model_validate(payload)

    assert [error["loc"] for error in refused.value.errors()] == [("unexpected",)]
    assert refused.value.errors()[0]["type"] == "extra_forbidden"


def test_error_a_false_acknowledgement_is_refused() -> None:
    payload = _recorded_payload() | {"projected": False}

    with pytest.raises(ValidationError) as refused:
        ModelSessionContentAppliedEvent.model_validate(payload)

    assert [error["loc"] for error in refused.value.errors()] == [("projected",)]


@pytest.mark.parametrize("missing", ["event_id", "rows_upserted"])
def test_error_a_payload_missing_its_own_facts_is_refused(missing: str) -> None:
    payload = _recorded_payload()
    del payload[missing]

    with pytest.raises(ValidationError) as refused:
        ModelSessionContentAppliedEvent.model_validate(payload)

    assert [error["loc"] for error in refused.value.errors()] == [(missing,)]
    assert refused.value.errors()[0]["type"] == "missing"
