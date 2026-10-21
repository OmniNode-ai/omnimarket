# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adapter's compaction half (OMN-18531 TA.11, plan section 6 steps 1 and 6).

The adapter mints a fresh invocation_id for every invocation and carries it on the
command; the compaction verdict installs a reply only when the echoed invocation_id
is its own and the messages hash to the echoed installed_messages_hash. Any
mismatch returns native.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from omnimarket.models.claude_code_binding import (
    EnumClientVerdictAction,
    ModelClientArgument,
)
from omnimarket.models.context_compaction import (
    ModelContextCompactionPackageReply,
    installed_messages_hash,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    INVOCATION_ID_SOURCE,
    HandlerClaudeCodeHookAdapter,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_compaction_verdict import (
    COMPACTION_VERDICT_COMPUTE,
    compute_compaction_verdict,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelHookAdapterRequest,
    ModelReceiptEvent,
)

from .conftest import EVENT_TOPIC, make_binding

if TYPE_CHECKING:
    from pydantic import JsonValue

pytestmark = pytest.mark.unit

HOOK_INPUT: dict[str, JsonValue] = {
    "session_id": "s-1",
    "cwd": "/work",
    "trigger": "manual",
}
MESSAGES = (
    ModelContextPackRenderMessage(role="user", text="the package body"),
    ModelContextPackRenderMessage(role="user", text="identity trailer"),
)


def compaction_binding(**overrides: object):
    fields: dict[str, object] = {
        "name": "compaction",
        "hook_event": "PreCompact",
        "matcher_tool": None,
        "command_type": "context_compaction_requested",
        "arguments": (
            ModelClientArgument(name="invocation_id", source=INVOCATION_ID_SOURCE),
            ModelClientArgument(name="session_id", source="$.session_id"),
            ModelClientArgument(name="cwd", source="$.cwd"),
            ModelClientArgument(name="trigger", source="$.trigger"),
        ),
        "verdict_compute": COMPACTION_VERDICT_COMPUTE,
    }
    fields.update(overrides)
    return make_binding(**fields)


def install_reply(invocation_id: UUID) -> dict[str, JsonValue]:
    reply = ModelContextCompactionPackageReply(
        invocation_id=invocation_id,
        action=EnumClientVerdictAction.INSTALL,
        messages=MESSAGES,
        installed_messages_hash=installed_messages_hash(MESSAGES),
    )
    payload: dict[str, JsonValue] = reply.model_dump(mode="json")
    return payload


def native_reply(invocation_id: UUID) -> dict[str, JsonValue]:
    reply = ModelContextCompactionPackageReply(
        invocation_id=invocation_id, action=EnumClientVerdictAction.NATIVE
    )
    payload: dict[str, JsonValue] = reply.model_dump(mode="json")
    return payload


def events(*payloads: dict[str, JsonValue]) -> tuple[ModelReceiptEvent, ...]:
    return tuple(ModelReceiptEvent(topic=EVENT_TOPIC, payload=p) for p in payloads)


class EchoingClient:
    """Replies to the published command, echoing the invocation_id the command carried."""

    def __init__(self, reply_for: object) -> None:
        self.reply_for = reply_for
        self.payloads: list[dict[str, JsonValue]] = []

    def publish_and_await(
        self, *, payload: dict[str, JsonValue], **_: object
    ) -> tuple[ModelReceiptEvent, ...]:
        self.payloads.append(payload)
        build = self.reply_for
        assert callable(build)
        return events(build(UUID(str(payload["invocation_id"]))))


def _handle(binding, client):
    return HandlerClaudeCodeHookAdapter(client).handle(
        ModelHookAdapterRequest(binding=binding, hook_input=HOOK_INPUT)
    )


class TestInvocationId:
    def test_a_fresh_invocation_id_is_minted_per_invocation_and_carried_on_the_command(
        self,
    ) -> None:
        client = EchoingClient(native_reply)
        first = _handle(compaction_binding(), client)
        second = _handle(compaction_binding(), client)
        sent = [UUID(str(p["invocation_id"])) for p in client.payloads]
        assert sent == [first.invocation_id, second.invocation_id]
        assert first.invocation_id != second.invocation_id
        assert first.invocation_id.version == 4

    def test_the_minted_id_is_the_only_field_not_drawn_from_the_hook_input(
        self,
    ) -> None:
        client = EchoingClient(native_reply)
        _handle(compaction_binding(), client)
        (payload,) = client.payloads
        assert set(payload) == {"invocation_id", "session_id", "cwd", "trigger"}
        assert (payload["session_id"], payload["cwd"], payload["trigger"]) == (
            "s-1",
            "/work",
            "manual",
        )

    def test_an_id_is_minted_even_when_the_binding_does_not_carry_it(self) -> None:
        binding = make_binding()
        recorded: list[dict[str, JsonValue]] = []

        class Recording:
            def publish_and_await(
                self, *, payload: dict[str, JsonValue], **_: object
            ) -> tuple[ModelReceiptEvent, ...]:
                recorded.append(payload)
                return events({"action": "proceed_native"})

        result = HandlerClaudeCodeHookAdapter(Recording()).handle(
            ModelHookAdapterRequest(
                binding=binding, hook_input={"session": {"id": "s"}, "input": {}}
            )
        )
        assert isinstance(result.invocation_id, UUID)
        assert "invocation_id" not in recorded[0]


class TestCompactionVerdict:
    def test_an_echoed_matching_package_installs_its_messages(self) -> None:
        result = _handle(compaction_binding(), EchoingClient(install_reply))
        assert result.client_verdict.action is EnumClientVerdictAction.INSTALL
        assert result.client_verdict.package_sha256 == installed_messages_hash(MESSAGES)
        assert result.hook_output == {
            "messages": [{"role": m.role, "text": m.text} for m in MESSAGES]
        }

    def test_another_invocations_reply_is_native(self) -> None:
        result = _handle(
            compaction_binding(), EchoingClient(lambda _: install_reply(uuid4()))
        )
        assert result.client_verdict.action is EnumClientVerdictAction.NATIVE
        assert result.hook_output is None

    def test_a_tampered_message_is_native(self) -> None:
        def tampered(invocation_id: UUID) -> dict[str, JsonValue]:
            payload = install_reply(invocation_id)
            messages = payload["messages"]
            assert isinstance(messages, list)
            payload["messages"] = [*messages[:-1], {"role": "user", "text": "edited"}]
            return payload

        result = _handle(compaction_binding(), EchoingClient(tampered))
        assert result.client_verdict.action is EnumClientVerdictAction.NATIVE

    def test_a_native_reply_is_native(self) -> None:
        result = _handle(compaction_binding(), EchoingClient(native_reply))
        assert result.client_verdict.action is EnumClientVerdictAction.NATIVE
        assert result.hook_output is None

    def test_a_reply_that_is_not_a_compaction_reply_is_native(self) -> None:
        def malformed(invocation_id: UUID) -> dict[str, JsonValue]:
            payload = install_reply(invocation_id)
            payload["messages"] = []
            return payload

        result = _handle(compaction_binding(), EchoingClient(malformed))
        assert result.client_verdict.action is EnumClientVerdictAction.NATIVE

    def test_no_reply_is_native(self) -> None:
        verdict = compute_compaction_verdict(compaction_binding(), (), uuid4())
        assert verdict.action is EnumClientVerdictAction.NATIVE

    def test_receipt_events_beside_the_reply_are_ignored(self) -> None:
        invocation_id = uuid4()
        receipts = events(
            {"candidate_id": str(uuid4()), "assembly_hash": "a" * 64},
            install_reply(invocation_id),
        )
        verdict = compute_compaction_verdict(
            compaction_binding(), receipts, invocation_id
        )
        assert verdict.action is EnumClientVerdictAction.INSTALL
        assert json.loads(verdict.text) == [m.model_dump() for m in MESSAGES]
