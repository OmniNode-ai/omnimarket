# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure-first: the agent message model refuses what is incomplete, unknown, naive or over long."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_agent_message_kind import EnumAgentMessageKind
from omnimarket.enums.enum_agent_message_platform import EnumAgentMessagePlatform
from omnimarket.models.agent_message.model_agent_message import (
    MAX_TEXT_CHARS,
    ModelAgentMessage,
)
from tests.unit.nodes.node_agent_message_effect.conftest import message_fields

pytestmark = pytest.mark.unit

REQUIRED = ("from", "to", "thread", "kind", "text", "sent_at", "platform")


def test_a_complete_message_is_accepted() -> None:
    message = ModelAgentMessage.model_validate(message_fields())
    assert message.sender == "lane-a"
    assert message.platform is EnumAgentMessagePlatform.CLAUDE
    assert message.kind is EnumAgentMessageKind.MESSAGE
    assert message.sent_at.utcoffset() is not None


@pytest.mark.parametrize("missing", REQUIRED)
def test_a_missing_field_is_refused(missing: str) -> None:
    fields = message_fields()
    del fields[missing]
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(fields)


@pytest.mark.parametrize("empty", ["from", "to", "thread", "text"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_or_blank_text_field_is_refused(empty: str, blank: str) -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(**{empty: blank}))


def test_an_unknown_platform_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(platform="mistral"))


def test_the_platform_set_is_closed() -> None:
    assert {p.value for p in EnumAgentMessagePlatform} == {
        "claude",
        "codex",
        "glm",
        "hermes",
        "gemini",
        "other",
    }


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(kind="shout"))


def test_the_kind_set_is_closed() -> None:
    assert {k.value for k in EnumAgentMessageKind} == {
        "message",
        "question",
        "answer",
        "notice",
    }


def test_a_naive_sent_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(sent_at="2026-11-23T13:30:00"))


def test_an_over_long_text_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(
            message_fields(text="x" * (MAX_TEXT_CHARS + 1))
        )
    assert ModelAgentMessage.model_validate(message_fields(text="x" * MAX_TEXT_CHARS))


def test_an_extra_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(cc="lane-c"))


def test_the_wire_form_spells_the_sender_from() -> None:
    wire = ModelAgentMessage.model_validate(message_fields()).model_dump(
        mode="json", by_alias=True
    )
    assert set(wire) == set(REQUIRED)
    assert wire["from"] == "lane-a"
