# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerPromptIntentClassify (OMN-19552).

The first draft of this module came from ``onex delegate`` run
afa53dd2-becd-43ce-8d4b-001b331030c5 and was corrected by hand: the draft's
fake classifier had the raw category and the typed class swapped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_intent_classification.handlers.handler_projection_intent_classification import (
    ModelIntentClassifiedEvent,
)
from omnimarket.nodes.node_prompt_intent_classify_compute import (
    HandlerPromptIntentClassify,
    ModelContentCapturedRecord,
    ModelIntentClassified,
    ModelPromptIntentVerdict,
)

_EMITTED_AT = "2026-09-25T11:40:00+00:00"


def _fake_classifier(prompt: str) -> ModelPromptIntentVerdict:
    assert prompt.strip(), "the handler must never classify blank text"
    return ModelPromptIntentVerdict(
        intent_category="debugging",
        intent_class="BUGFIX",
        confidence=0.9,
        keywords=["fix"],
    )


def _record(**overrides: object) -> ModelContentCapturedRecord:
    fields: dict[str, object] = {
        "session_id": "sess-1",
        "correlation_id": "corr-1",
        "content_kind": "prompt",
        "content": "fix the failing test",
        "chunk_index": 0,
        "chunk_count": 1,
        "emitted_at": _EMITTED_AT,
        "actor": "claude",
    }
    fields.update(overrides)
    return ModelContentCapturedRecord.model_validate(fields)


@pytest.fixture
def handler() -> HandlerPromptIntentClassify:
    return HandlerPromptIntentClassify(classifier=_fake_classifier)


@pytest.mark.unit
def test_prompt_record_yields_intent_classified_event(
    handler: HandlerPromptIntentClassify,
) -> None:
    result = handler.handle(_record())

    assert isinstance(result, ModelIntentClassified)
    assert result.session_id == "sess-1"
    assert result.correlation_id == "corr-1"
    assert result.intent_class == "BUGFIX"
    assert result.intent_category == "debugging"
    assert result.confidence == 0.9
    assert result.keywords == ["fix"]
    # Pure: the emission time is the capture record's, never a clock read.
    assert result.emitted_at == _EMITTED_AT
    assert result.agent_source == "claude"


@pytest.mark.unit
def test_record_parses_from_wire_payload_with_unknown_fields() -> None:
    wire = {
        "session_id": "sess-1",
        "correlation_id": "corr-1",
        "content_kind": "prompt",
        "content": "add a tenant listing endpoint",
        "chunk_index": 0,
        "chunk_count": 1,
        "emitted_at": _EMITTED_AT,
        "content_sha256": "ab" * 32,
        "producer_redaction": {},
        "lane": "some-lane",
        "redaction_state": "clean",
    }
    record = ModelContentCapturedRecord.model_validate(wire)
    assert record.content_kind == "prompt"


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"content_kind": "tool_response"}, id="skip_non_prompt"),
        pytest.param({"content_kind": "assistant_reply"}, id="skip_assistant_reply"),
        pytest.param({"chunk_index": 1, "chunk_count": 2}, id="skip_non_first_chunk"),
        pytest.param({"content": "   "}, id="skip_blank_content"),
        pytest.param({"content": None}, id="skip_missing_content"),
        pytest.param({"correlation_id": None}, id="skip_missing_correlation"),
        pytest.param({"correlation_id": ""}, id="skip_empty_correlation"),
    ],
)
def test_skip_records_that_are_not_classifiable_prompts(
    handler: HandlerPromptIntentClassify, overrides: dict[str, object]
) -> None:
    assert handler.handle(_record(**overrides)) is None


@pytest.mark.unit
def test_agent_source_follows_the_capturing_actor(
    handler: HandlerPromptIntentClassify,
) -> None:
    result = handler.handle(_record(actor="codex"))
    assert result is not None
    assert result.agent_source == "codex"


@pytest.mark.unit
def test_event_validates_against_projection_model(
    handler: HandlerPromptIntentClassify,
) -> None:
    result = handler.handle(_record())
    assert result is not None

    event = ModelIntentClassifiedEvent.model_validate(result.model_dump(mode="json"))

    assert event.correlation_id == "corr-1"
    assert event.session_id == "sess-1"
    assert event.intent_class == "BUGFIX"
    assert event.confidence == 0.9
    assert event.keywords == ["fix"]
    assert event.emitted_at == _EMITTED_AT
    assert event.agent_source == "claude"


_NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_INTENT_CLASSIFIED_TOPIC = "onex.evt.omniintelligence.intent-classified.v1"
_CONTENT_CAPTURED_TOPIC = "onex.evt.omniclaude.content-captured.v1"


def _event_bus(node_dir: str) -> dict[str, list[str]]:
    contract = yaml.safe_load((_NODES_DIR / node_dir / "contract.yaml").read_text())
    event_bus: dict[str, list[str]] = contract["event_bus"]
    return event_bus


@pytest.mark.unit
def test_contract_publishes_the_topic_the_projection_subscribes_to() -> None:
    ours = _event_bus("node_prompt_intent_classify_compute")
    projection = _event_bus("node_projection_intent_classification")

    assert ours["subscribe_topics"] == [_CONTENT_CAPTURED_TOPIC]
    assert ours["publish_topics"] == [_INTENT_CLASSIFIED_TOPIC]
    assert _INTENT_CLASSIFIED_TOPIC in projection["subscribe_topics"]


@pytest.mark.unit
@pytest.mark.skipif(
    importlib.util.find_spec("omniintelligence") is None,
    reason="omniintelligence is a runtime peer, installed in the runtime image only",
)
def test_default_classifier_uses_the_omniintelligence_library() -> None:
    result = HandlerPromptIntentClassify().handle(
        _record(content="fix the failing test, it raises KeyError")
    )
    assert result is not None
    assert result.intent_class == "BUGFIX"
    assert result.intent_category == "debugging"
