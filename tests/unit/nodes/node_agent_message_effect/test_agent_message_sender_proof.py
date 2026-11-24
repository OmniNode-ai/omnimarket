# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure-first: the typed message carries a sender proof and every reader refuses what it cannot verify."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_agent_message_operation import EnumAgentMessageOperation
from omnimarket.enums.enum_agent_message_refusal import EnumAgentMessageRefusal
from omnimarket.enums.enum_agent_message_verification_reason import (
    EnumAgentMessageVerificationReason as Reason,
)
from omnimarket.handlers.agent_message.signature import (
    canonical_signing_bytes,
    sign_message,
)
from omnimarket.models.agent_message.model_agent_message import (
    MAX_TEXT_CHARS,
    ModelAgentMessage,
)
from omnimarket.models.agent_message.model_agent_signing_key import (
    ModelAgentSigningKey,
)
from omnimarket.nodes.node_agent_message_effect.handlers.agent_message_verifier import (
    AgentMessageVerificationError,
    AgentMessageVerifier,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
    message_event_id,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_request import (
    ModelAgentMessageRequest,
)
from omnimarket.nodes.node_agent_message_effect.models.model_agent_message_result import (
    ModelAgentMessageResult,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from tests.unit.nodes.node_agent_message_effect.agent_directory_fakes import (
    FakeAgentDirectory,
    TestAgent,
    make_agent,
)
from tests.unit.nodes.node_agent_message_effect.conftest import FakeBus, message_fields

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_agent_message_effect"
)
SEND = EnumAgentMessageOperation.SEND
READ = EnumAgentMessageOperation.READ


@pytest.fixture
def alice() -> TestAgent:
    return make_agent("lane-a", "User:dev-jonah-hp-gen8")


@pytest.fixture
def mallory() -> TestAgent:
    return make_agent("lane-m", "User:dev-jonah-omnipc2")


@pytest.fixture
def directory(alice: TestAgent, mallory: TestAgent) -> FakeAgentDirectory:
    return FakeAgentDirectory(
        registered={a.name: a.registered() for a in (alice, mallory)},
        held_keys={alice.name: alice.signing_key()},
    )


def _handler(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, *, signs: bool = False
) -> HandlerAgentMessage:
    emitter = HandlerEventEmitEffect(publish_adapter=bus, spool_dir=tmp_path / "spool")
    return HandlerAgentMessage(
        emitter=emitter,
        bus=bus,
        registry=directory,
        signing_keys=directory if signs else None,
        read_window_seconds=0.05,
        quiet_gap_seconds=0.02,
    )


def _signed(
    agent: TestAgent,
    after: dict[str, object] | None = None,
    **before: object,
) -> dict[str, object]:
    """A wire message `agent` signed; `before` sets fields that are signed, `after` edits them once signed."""
    fields = message_fields(
        **{
            "from": agent.name,
            "message_id": "m-1",
            "message_schema_version": 1,
            **before,
        }
    )
    message = ModelAgentMessage.model_validate(fields)
    proof = sign_message(message, agent.signing_key())
    fields["sender_proof"] = proof.model_dump(mode="json")
    fields.update(after or {})
    return fields


def _publish_raw(bus: FakeBus, wire: dict[str, object]) -> None:
    """A record put on the topic by anything that holds a broker login, bypassing the node's send."""
    bus.records.append(
        ("onex.evt.omnimarket.agent-message.v1", json.dumps(wire).encode())
    )


def _read(handler: HandlerAgentMessage, to: str = "lane-b") -> ModelAgentMessageResult:
    return asyncio.run(handler.handle(ModelAgentMessageRequest(operation=READ, to=to)))


# ---- the typed model ----


def test_the_new_fields_are_optional_so_the_legacy_shape_still_validates() -> None:
    message = ModelAgentMessage.model_validate(message_fields())
    assert message.message_id is None
    assert message.message_schema_version is None
    assert message.sender_proof is None


def test_the_message_model_is_frozen_and_closed() -> None:
    message = ModelAgentMessage.model_validate(message_fields(message_id="m-1"))
    with pytest.raises(ValidationError):
        message.__setattr__("message_id", "m-2")
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(unexpected="x"))


def test_a_blank_message_id_or_a_zero_schema_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(message_id="  "))
    with pytest.raises(ValidationError):
        ModelAgentMessage.model_validate(message_fields(message_schema_version=0))


def test_the_signed_bytes_cover_every_typed_field() -> None:
    base = ModelAgentMessage.model_validate(
        message_fields(message_id="m-1", message_schema_version=1)
    )
    signed_over = canonical_signing_bytes(base)
    for field, other in (
        ("message_id", "m-2"),
        ("message_schema_version", 2),
        ("from", "lane-z"),
        ("to", "lane-z"),
        ("thread", "thread-2"),
        ("kind", "notice"),
        ("text", "something else"),
        ("sent_at", "2026-11-23T13:31:00+00:00"),
        ("platform", "codex"),
    ):
        fields = message_fields(message_id="m-1", message_schema_version=1)
        fields[field] = other
        changed = ModelAgentMessage.model_validate(fields)
        assert canonical_signing_bytes(changed) != signed_over, field


# ---- AC1: a spoofed sender is refused by every reader ----


def test_spoofed_sender_refused_when_the_signer_is_another_registered_agent(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, mallory: TestAgent
) -> None:
    # lane-m signs with its own key but declares lane-a as the sender.
    fields = message_fields(
        **{"from": "lane-a", "message_id": "m-9", "message_schema_version": 1}
    )
    message = ModelAgentMessage.model_validate(fields)
    fields["sender_proof"] = sign_message(message, mallory.signing_key()).model_dump(
        mode="json"
    )
    _publish_raw(bus, fields)
    result = _read(_handler(bus, tmp_path, directory))
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.SENDER_MISMATCH]


def test_spoofed_sender_refused_when_the_signature_is_forged_under_the_real_key_id(
    bus: FakeBus,
    tmp_path: Path,
    directory: FakeAgentDirectory,
    alice: TestAgent,
    mallory: TestAgent,
) -> None:
    fields = message_fields(
        **{"from": "lane-a", "message_id": "m-9", "message_schema_version": 1}
    )
    message = ModelAgentMessage.model_validate(fields)
    stolen_id = ModelAgentSigningKey(
        key_id=alice.key_id, seed=mallory.signing_key().seed
    )
    forged = sign_message(message, stolen_id)
    fields["sender_proof"] = forged.model_dump(mode="json")
    _publish_raw(bus, fields)
    result = _read(_handler(bus, tmp_path, directory))
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.SENDER_MISMATCH]


def test_a_message_edited_after_signing_is_refused_as_sender_mismatch(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, alice: TestAgent
) -> None:
    _publish_raw(bus, _signed(alice, after={"text": "a different body"}))
    result = _read(_handler(bus, tmp_path, directory))
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.SENDER_MISMATCH]


def test_the_producer_principal_must_equal_the_senders_registered_principal(
    directory: FakeAgentDirectory, alice: TestAgent
) -> None:
    message = ModelAgentMessage.model_validate(_signed(alice))
    verifier = AgentMessageVerifier(directory)
    asyncio.run(verifier.verify(message, authenticated_principal=alice.principal))
    with pytest.raises(AgentMessageVerificationError) as raised:
        asyncio.run(
            verifier.verify(message, authenticated_principal="User:dev-jonah-omnipc2")
        )
    assert raised.value.reason is Reason.SENDER_MISMATCH


def test_the_good_signed_message_is_read_with_its_proof(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, alice: TestAgent
) -> None:
    _publish_raw(bus, _signed(alice))
    result = _read(_handler(bus, tmp_path, directory))
    assert [m.sender for m in result.messages] == ["lane-a"]
    assert result.messages[0].sender_proof is not None
    assert result.rejections == ()


# ---- AC2: unknown sender, malformed and oversize bodies fail with a typed reason ----


def test_an_unknown_sender_is_refused(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    ghost = make_agent("ghost", "User:nobody")
    _publish_raw(bus, _signed(ghost))
    result = _read(_handler(bus, tmp_path, directory))
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.UNKNOWN_SENDER]


@pytest.mark.parametrize(
    "missing", ["to", "thread", "kind", "text", "sent_at", "platform", "from"]
)
def test_a_message_missing_a_typed_field_is_malformed(
    bus: FakeBus,
    tmp_path: Path,
    directory: FakeAgentDirectory,
    alice: TestAgent,
    missing: str,
) -> None:
    wire = _signed(alice)
    del wire[missing]
    _publish_raw(bus, wire)
    result = _read(_handler(bus, tmp_path, directory), to="lane-b")
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.MALFORMED_MESSAGE]
    assert result.skipped == 1


def test_an_oversize_body_is_refused_with_its_own_reason(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, alice: TestAgent
) -> None:
    _publish_raw(bus, _signed(alice, after={"text": "x" * (MAX_TEXT_CHARS + 1)}))
    result = _read(_handler(bus, tmp_path, directory))
    assert [r.reason for r in result.rejections] == [Reason.OVERSIZE_BODY]


def test_send_refuses_an_oversize_body_and_publishes_nothing(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    handler = _handler(bus, tmp_path, directory)
    result = asyncio.run(
        handler.handle(
            ModelAgentMessageRequest(
                operation=SEND, message=message_fields(text="x" * (MAX_TEXT_CHARS + 1))
            )
        )
    )
    assert result.accepted is False
    assert result.refusal is EnumAgentMessageRefusal.INVALID_MESSAGE
    assert result.reason is Reason.OVERSIZE_BODY
    assert bus.records == []


# ---- the cutover: the legacy wire shape is read until every reader is on the new one ----


def test_a_legacy_message_is_read_while_legacy_is_allowed(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    _publish_raw(bus, message_fields())
    result = _read(_handler(bus, tmp_path, directory))
    assert [m.sender for m in result.messages] == ["lane-a"]
    assert result.unverified == 1


def test_a_legacy_message_is_refused_once_the_cutover_flag_is_off(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    directory.legacy_allowed = False
    _publish_raw(bus, message_fields())
    result = _read(_handler(bus, tmp_path, directory))
    assert result.messages == ()
    assert [r.reason for r in result.rejections] == [Reason.MISSING_PROOF]


# ---- send signs with the key this host holds; ids are stable across retries ----


def test_send_signs_with_the_senders_key_and_the_reader_verifies_it(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    handler = _handler(bus, tmp_path, directory, signs=True)
    sent = asyncio.run(
        handler.handle(
            ModelAgentMessageRequest(
                operation=SEND, message=message_fields(**{"from": "lane-a"})
            )
        )
    )
    assert sent.accepted is True
    payload = json.loads(bus.records[0][1])
    assert payload["sender_proof"]["key_id"] == "lane-a-key-1"
    assert payload["message_schema_version"] == 1
    assert payload["message_id"]
    assert [m.sender for m in _read(_handler(bus, tmp_path, directory)).messages] == [
        "lane-a"
    ]


def test_send_with_no_key_for_the_declared_sender_is_refused_once_legacy_is_off(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory
) -> None:
    directory.legacy_allowed = False
    handler = _handler(bus, tmp_path, directory, signs=True)
    result = asyncio.run(
        handler.handle(
            ModelAgentMessageRequest(
                operation=SEND, message=message_fields(**{"from": "lane-m"})
            )
        )
    )
    assert result.accepted is False
    assert result.reason is Reason.UNKNOWN_SENDER
    assert bus.records == []


def test_the_event_id_is_stable_across_a_retry_with_a_new_sent_at() -> None:
    first = ModelAgentMessage.model_validate(message_fields(message_id="m-1"))
    retry = ModelAgentMessage.model_validate(
        message_fields(message_id="m-1", sent_at="2026-11-23T13:31:09+00:00")
    )
    assert message_event_id(first) == message_event_id(retry)
    other = ModelAgentMessage.model_validate(message_fields(message_id="m-2"))
    assert message_event_id(first) != message_event_id(other)


# ---- a message to "all" reaches every recipient's read ----


def test_a_message_to_all_is_returned_to_any_recipient(
    bus: FakeBus, tmp_path: Path, directory: FakeAgentDirectory, alice: TestAgent
) -> None:
    _publish_raw(bus, _signed(alice, to="all"))
    messages = _read(_handler(bus, tmp_path, directory), to="lane-z").messages
    assert [m.to for m in messages] == ["all"]


# ---- the contract declares the message ----


def test_the_topic_contract_declares_the_typed_message_and_its_refusals() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    declared = contract["message_contract"]
    assert (
        declared["model"]
        == "omnimarket.models.agent_message.model_agent_message.ModelAgentMessage"
    )
    wire_fields = {f["wire"] for f in declared["fields"]}
    assert wire_fields == {
        "message_id",
        "from",
        "to",
        "thread",
        "kind",
        "text",
        "sent_at",
        "platform",
        "message_schema_version",
        "sender_proof",
    }
    assert set(declared["refusal_reasons"]) == {r.value for r in Reason}
    assert declared["sender_proof"]["verified_against"] == "settings_store_registry"
