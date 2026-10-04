# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Label dispatch fails closed without its required source or rating evidence."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from omnimarket.events.delegation_eval import ModelLabelRecordRequest
from omnimarket.nodes.node_delegation_eval_orchestrator.handlers import (
    HandlerDelegationEvalOrchestrator,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.protocols import (
    ProtocolDelegationEventSnapshot,
)

pytestmark = pytest.mark.unit


def _label() -> dict[str, object]:
    return {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "correlation_id": "edge-call",
        "attempt_index": 0,
        "label": "adequate",
        "rater_role": "human",
        "rubric_version": "v1",
        "stratum": "summarization/accepted",
        "computed_facts": {"passed": True},
    }


def test_missing_snapshot_source_refuses_dispatch() -> None:
    request = ModelLabelRecordRequest.model_validate(_label())

    with pytest.raises(
        RuntimeError, match=r"^tenant-scoped snapshot source is required for dispatch$"
    ):
        HandlerDelegationEvalOrchestrator().handle(request)


@pytest.mark.parametrize(
    "field", ["label", "rater_role", "rubric_version", "computed_facts"]
)
def test_missing_rating_evidence_refuses_before_snapshot_read(field: str) -> None:
    # The label-record contract calls these fields rating evidence; it has no
    # separate `criteria` field. Required evidence is validated before dispatch.
    source = Mock(spec=ProtocolDelegationEventSnapshot)
    handler = HandlerDelegationEvalOrchestrator(source)
    payload = _label()
    del payload[field]

    with pytest.raises(ValidationError) as exc:
        handler.handle(ModelLabelRecordRequest.model_validate(payload))

    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        ((field,), "missing")
    ]
    source.get_snapshot.assert_not_called()
