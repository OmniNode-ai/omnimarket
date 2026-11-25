# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A good message goes through send and read on a shared bus, and a spoofed one sent by another login is refused."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason as Reason,
)
from omnimarket.models.agent_message.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from tests.unit.nodes.node_agent_message_effect.agent_directory_fakes import (
    FakeAgentDirectory,
    make_agent,
)
from tests.unit.nodes.node_agent_message_effect.conftest import FakeBus, message_fields

pytestmark = pytest.mark.integration


def _handler(
    bus: FakeBus, spool: Path, directory: FakeAgentDirectory, *, signs: bool
) -> HandlerAgentMessage:
    return HandlerAgentMessage(
        emitter=HandlerEventEmitEffect(publish_adapter=bus, spool_dir=spool),
        bus=bus,
        registry=directory,
        signing_keys=directory if signs else None,
        read_window_seconds=0.05,
        quiet_gap_seconds=0.02,
    )


def test_registered_login_is_read_back_and_the_spoofing_login_is_not(
    tmp_path: Path,
) -> None:
    alice = make_agent("lane-a", "User:dev-jonah-hp-gen8")
    mallory = make_agent("lane-m", "User:dev-jonah-omnipc2")
    registry = {a.name: a.registered() for a in (alice, mallory)}
    bus = FakeBus()
    # Host one holds lane-a's key; host two holds only lane-m's key and declares lane-a.
    host_one = FakeAgentDirectory(registry, {"lane-a": alice.signing_key()})
    host_two = FakeAgentDirectory(registry, {"lane-m": mallory.signing_key()})
    good = asyncio.run(
        _handler(bus, tmp_path / "one", host_one, signs=True).handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.SEND,
                message=message_fields(
                    **{"from": "lane-a", "text": "from the registered login"}
                ),
            )
        )
    )
    assert good.accepted is True
    # host two cannot sign as lane-a; its message goes out under lane-m's key with lane-a declared.
    spoof_fields = message_fields(**{"from": "lane-a", "text": "from the other login"})
    host_two_signer = _handler(bus, tmp_path / "two", host_two, signs=True)
    from omnimarket.handlers.agent_message.signature import sign_message
    from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage

    fields = {**spoof_fields, "message_id": "spoof-1", "message_schema_version": 1}
    fields["sender_proof"] = sign_message(
        ModelAgentMessage.model_validate(fields),
        mallory.signing_key(),
    ).model_dump(mode="json")
    spoof = asyncio.run(
        host_two_signer.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.SEND, message=fields
            )
        )
    )
    assert spoof.accepted is True  # the writer can publish; the readers decide
    reader = _handler(
        bus, tmp_path / "reader", FakeAgentDirectory(registry), signs=False
    )
    result = asyncio.run(
        reader.handle(
            ModelAgentMessageRequest(
                operation=EnumAgentMessageOperation.READ, to="lane-b"
            )
        )
    )
    assert [m.text for m in result.messages] == ["from the registered login"]
    assert [r.reason for r in result.rejections] == [Reason.SENDER_MISMATCH]
