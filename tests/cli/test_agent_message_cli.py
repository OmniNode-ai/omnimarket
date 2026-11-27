# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex agent-message send`` and ``read`` against a fake node."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_agent_message_client_effect.cli import agent_message_group
from tests.cli.agent_message_fakes import FIXED_NOW, FakeAgentMessageNode, make_runtime
from tests.nodes.node_architecture_discovery_orchestrator.broker_overlay_support import (
    install_broker_overlays,
)

pytestmark = pytest.mark.unit

_SEND = [
    "send",
    "--to",
    "codex:review",
    "--thread",
    "t-1",
    "--kind",
    "question",
    "--platform",
    "claude",
    "--text",
    "ping",
]


def _invoke(
    node: FakeAgentMessageNode, args: list[str], env: dict[str, str] | None = None
):
    return CliRunner().invoke(
        agent_message_group, args, obj=make_runtime(node, env), catch_exceptions=False
    )


def test_send_then_read_round_trip() -> None:
    node = FakeAgentMessageNode()
    sent = _invoke(node, [*_SEND, "--json"], {"ONEX_LANE": "agentbus-client-1310"})
    assert sent.exit_code == 0, sent.output
    message = json.loads(sent.output)["sent"]
    assert message["from"] == "agentbus-client-1310"
    assert message["sent_at"] == FIXED_NOW.isoformat().replace("+00:00", "Z")
    assert node.sends == 1

    read = _invoke(node, ["read", "--to", "codex:review", "--thread", "t-1", "--json"])
    assert read.exit_code == 0, read.output
    messages = json.loads(read.output)["messages"]
    assert [m["message_id"] for m in messages] == [message["message_id"]]
    assert messages[0]["text"] == "ping"


def test_read_filters_by_thread_since_and_limit() -> None:
    node = FakeAgentMessageNode()
    for thread in ("t-1", "t-2"):
        _invoke(node, [*_SEND[:4], thread, *_SEND[5:]])
    only_t2 = json.loads(
        _invoke(
            node, ["read", "--to", "codex:review", "--thread", "t-2", "--json"]
        ).output
    )
    assert [m["thread"] for m in only_t2["messages"]] == ["t-2"]
    later = _invoke(
        node,
        ["read", "--to", "codex:review", "--since", "2026-11-23T14:00:00Z", "--json"],
    )
    assert json.loads(later.output) == {
        "messages": [],
        "unverified": 0,
        "rejections": [],
    }
    limited = _invoke(node, ["read", "--to", "codex:review", "--limit", "1", "--json"])
    assert len(json.loads(limited.output)["messages"]) == 1


def test_plain_text_output_and_text_from_file(tmp_path: Path) -> None:
    node = FakeAgentMessageNode()
    body = tmp_path / "body.txt"
    body.write_text("line one\nline two\n", encoding="utf-8")
    args = [*_SEND[:-2], "--text", f"-@{body}"]
    sent = _invoke(node, args)
    assert sent.exit_code == 0
    assert sent.output.startswith("sent ")
    read = _invoke(node, ["read", "--to", "codex:review"])
    assert "thread=t-1" in read.output
    assert "  line two" in read.output


def test_sender_defaults_to_platform_identity_without_a_lane() -> None:
    node = FakeAgentMessageNode()
    _invoke(node, _SEND)
    assert node.topic[0].sender == "claude:claude"
    _invoke(node, _SEND, {"ONEX_AGENT_IDENTITY": "codex:review-7"})
    assert node.topic[1].sender == "codex:review-7"


@pytest.mark.parametrize(
    ("args", "code", "exit_code"),
    [
        (
            [
                "send",
                "--thread",
                "t",
                "--kind",
                "question",
                "--platform",
                "claude",
                "--text",
                "x",
            ],
            "missing_recipient",
            2,
        ),
        ([*_SEND[:-1], "   "], "empty_text", 2),
        (
            [
                "send",
                "--to",
                "a",
                "--thread",
                "t",
                "--kind",
                "question",
                "--platform",
                "claude",
            ],
            "empty_text",
            2,
        ),
        ([*_SEND[:6], "gizmo", *_SEND[7:]], "unknown_kind", 2),
        ([*_SEND[:8], "grok", *_SEND[9:]], "unknown_platform", 2),
        ([*_SEND[:2], "grok:x", *_SEND[3:]], "unknown_platform", 2),
        ([*_SEND[:4], "", *_SEND[5:]], "missing_thread", 2),
        (["read"], "missing_recipient", 2),
        (["read", "--to", "a", "--since", "yesterday"], "bad_since", 2),
        (["read", "--to", "a", "--limit", "0"], "bad_limit", 2),
    ],
)
def test_typed_refusals_exit_nonzero_with_no_stack_trace(
    args: list[str], code: str, exit_code: int
) -> None:
    node = FakeAgentMessageNode()
    result = CliRunner().invoke(agent_message_group, args, obj=make_runtime(node))
    assert result.exit_code == exit_code, result.output
    assert f"refused: {code}:" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)
    assert node.sends == 0
    assert node.reads == 0


def test_json_refusal_is_parseable() -> None:
    node = FakeAgentMessageNode()
    result = CliRunner().invoke(
        agent_message_group, ["read", "--json"], obj=make_runtime(node)
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["refused"] == "missing_recipient"


def test_undeclared_broker_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ONEX_AGENT_MESSAGE_BUS_LANE", "OMNIBASE_PATH"):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(agent_message_group, [*_SEND, "--bus", "kafka"])
    assert result.exit_code == 3, result.output
    assert "refused: undeclared_broker:" in result.output
    assert "Traceback" not in result.output


def test_a_node_with_no_declared_broker_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The node dials the broker its own overlay declares; with none declared the client answers undeclared_broker, never a trace.
    # The test injects its own overlay root (an overlay with no event_bus) and never reads the host's.
    for name in (
        "ONEX_EMIT_EFFECT_SPOOL_ONLY",
        "KAFKA_BOOTSTRAP_SERVERS",
        "ONEX_SKILL_OVERLAY_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)
    install_broker_overlays(
        tmp_path, monkeypatch, None, nodes=("node_agent_message_effect",)
    )
    result = CliRunner().invoke(agent_message_group, [*_SEND, "--bus", "inmemory"])
    assert result.exit_code == 3, result.output
    assert "refused: undeclared_broker:" in result.output
    assert "Traceback" not in result.output


def _node_with_refusals() -> FakeAgentMessageNode:
    from omnimarket.enums.enum_agent_message_verification_reason import (
        EnumAgentMessageVerificationReason,
    )
    from omnimarket.models.agent_message.model_agent_message_rejection import (
        ModelAgentMessageRejection,
    )

    node = FakeAgentMessageNode()
    node.unverified = 2
    node.rejections = [
        ModelAgentMessageRejection(
            reason=EnumAgentMessageVerificationReason.SENDER_MISMATCH,
            detail="signed by another sender's key",
            sender="lane-b",
            message_id="m-spoof",
        )
    ]
    return node


def test_read_json_carries_the_typed_refusals_and_the_legacy_count() -> None:
    read = _invoke(_node_with_refusals(), ["read", "--to", "codex:review", "--json"])
    assert read.exit_code == 0, read.output
    body = json.loads(read.output)
    assert body["unverified"] == 2
    assert [r["reason"] for r in body["rejections"]] == ["sender_mismatch"]
    assert body["rejections"][0]["sender"] == "lane-b"
    assert "text" not in body["rejections"][0]


def test_read_text_names_each_refused_record_by_its_reason() -> None:
    read = _invoke(_node_with_refusals(), ["read", "--to", "codex:review"])
    assert read.exit_code == 0, read.output
    assert "refused: sender_mismatch: sender=lane-b id=m-spoof" in read.output
    assert "2 message(s) carry no sender proof" in read.output
