# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19790: no planted snapshot credential crosses the label-event boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_eval_orchestrator.handlers import (
    HandlerDelegationEvalOrchestrator,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelDelegationEventSnapshot,
    ModelLabelRecordRequest,
)
from omnimarket.nodes.node_projection_delegation_eval.handlers import (
    HandlerProjectionDelegationEval,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
)

pytestmark = pytest.mark.unit
_TOPIC = "onex.evt.omnimarket.delegation-eval-item-labelled.v1"  # onex-topic-allow: asserted against the contract


class _Source:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, int]] = []

    def get_snapshot(
        self, correlation_id: str, attempt_index: int
    ) -> ModelDelegationEventSnapshot:
        self.calls.append((correlation_id, attempt_index))
        return ModelDelegationEventSnapshot(
            prompt=self.text,
            response=self.text,
            task_class="code",
            gate_verdict="pass",
            deciding_check="tests",
        )


@pytest.mark.parametrize(
    ("planted", "secret"),
    [
        ("Authorization: Bearer planted-token.123", "planted-token.123"),
        ("sk-proj-planted123456789", "sk-proj-planted123456789"),
        ("ghp_planted123456789", "ghp_planted123456789"),
        ("xoxb-123-456-planted", "xoxb-123-456-planted"),
        ("xoxp-123-456-planted", "xoxp-123-456-planted"),
        ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
        ("password=hunter2", "hunter2"),
        ('api_key = "two word secret"', "two word secret"),
        ("PASSWORD='another secret'", "another secret"),
        ('"api_key": "json-secret"', "json-secret"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nprivate-material\n-----END RSA PRIVATE KEY-----",
            "private-material",
        ),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nssh-material\n-----END OPENSSH PRIVATE KEY-----",
            "ssh-material",
        ),
        ("https://x-access-token:url-secret@example.com/repo", "url-secret"),
    ],
)
def test_eval_label_snapshot_scrubbed(planted: str, secret: str) -> None:
    source = _Source(f"Keep this useful context.\n{planted}\nAnd this explanation.")
    request = ModelLabelRecordRequest(
        tenant_id="11111111-1111-1111-1111-111111111111",
        correlation_id="call-1",
        attempt_index=0,
        label="correct",
        rater_role="human",
        rubric_version="v1",
        stratum="hard",
        computed_facts={"passed": True},
    )
    result = HandlerDelegationEvalOrchestrator(source).handle(request)
    assert secret not in result.model_dump_json()
    for text in (result.payload.prompt_snapshot, result.payload.response_snapshot):
        assert "Keep this useful context." in text
        assert "And this explanation." in text
    assert source.calls == [("call-1", 0)]
    assert result.topic == _TOPIC
    # Returned event is directly consumable by the rule-7a fold.
    projected = HandlerProjectionDelegationEval().handle(
        ModelDelegationEvalProjectionRequest.model_validate(result.payload.model_dump())
    )
    assert projected.rows[0].item_key == "call-1:0"
    assert projected.rows[0].label == request.label
    assert projected.rows[0].task_class == "code"


def test_eval_label_snapshot_scrubbed_contract_publish_topic() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_delegation_eval_orchestrator/contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["publish_topics"] == [_TOPIC]
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["handler_routing"]["handlers"][0]["operation"] == "label-record"


def test_eval_label_snapshot_scrubbed_secret_and_token_keys() -> None:
    from omnimarket.nodes.node_delegation_eval_orchestrator.scrubber import (
        scrub_snapshot,
    )

    scrubbed = scrub_snapshot("client_secret=abc123XYZ and access_token: 'tok999' ok")
    assert "abc123XYZ" not in scrubbed
    assert "tok999" not in scrubbed
    assert scrubbed.endswith("ok")
