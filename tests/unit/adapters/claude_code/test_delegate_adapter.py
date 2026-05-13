# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the Claude Code delegation adapter."""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.adapters.claude_code.delegate import (
    DelegationDispatchAdapter,
    DelegationTopics,
    build_delegation_payload,
    main,
    resolve_topics_from_contract,
)


@pytest.mark.unit
def test_resolve_topics_from_named_fields() -> None:
    topics = resolve_topics_from_contract()
    assert isinstance(topics, DelegationTopics)
    assert topics.command_topic == "onex.cmd.omnimarket.delegate-skill.v1"
    assert topics.success_topic == "onex.evt.omnimarket.delegate-skill-completed.v1"
    assert topics.failure_topic == "onex.evt.omnimarket.delegate-skill-failed.v1"
    assert topics.default_timeout_ms == 300000
    assert topics.max_timeout_ms == 900000


@pytest.mark.unit
def test_build_delegation_payload_includes_all_fields() -> None:
    payload = build_delegation_payload(
        prompt="Write tests",
        task_type="test",
        source="claude-code",
        cwd="/path",
    )
    assert payload["prompt"] == "Write tests"
    assert payload["task_type"] == "test"
    assert payload["source"] == "claude-code"
    assert payload["cwd"] == "/path"
    assert "correlation_id" in payload
    # correlation_id must be a valid UUID string
    UUID(str(payload["correlation_id"]))


@pytest.mark.unit
def test_build_delegation_payload_rejects_non_uuid_correlation_id() -> None:
    with pytest.raises(ValueError, match="correlation_id"):
        build_delegation_payload(
            prompt="Test",
            task_type="test",
            source="claude-code",
            correlation_id="not-a-uuid",
        )


@pytest.mark.unit
def test_build_delegation_payload_rejects_invalid_task_type() -> None:
    with pytest.raises(ValueError, match="task_type"):
        build_delegation_payload(
            prompt="Test",
            task_type="invalid",
            source="claude-code",
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad_max_tokens", [0, -1, -2048])
def test_build_delegation_payload_rejects_non_positive_max_tokens(
    bad_max_tokens: int,
) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        build_delegation_payload(
            prompt="Test",
            task_type="test",
            source="claude-code",
            max_tokens=bad_max_tokens,
        )


@pytest.mark.unit
def test_build_delegation_payload_rejects_bool_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        build_delegation_payload(
            prompt="Test",
            task_type="test",
            source="claude-code",
            max_tokens=True,  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_dispatch_sync_subscribes_to_failure_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter must pass the failure topic alongside the success topic."""
    captured: dict[str, object] = {}

    class _FakeRuntimeAdapter:
        def __init__(self, **_: object) -> None:
            pass

        def dispatch_sync(self, **kwargs: object) -> object:
            captured.update(kwargs)

            class _Resp:
                def model_dump(self, **__: object) -> dict[str, object]:
                    return {"ok": True}

            return _Resp()

    import omnimarket.adapters.codex.runtime_client as runtime_client

    monkeypatch.setattr(
        runtime_client, "CodexRuntimeRequestAdapter", _FakeRuntimeAdapter
    )

    adapter = DelegationDispatchAdapter()
    adapter.dispatch_sync(prompt="Test", task_type="test", source="claude-code")

    assert captured["response_topic"] == (
        "onex.evt.omnimarket.delegate-skill-completed.v1"
    )
    assert captured["additional_response_topics"] == (
        ("onex.evt.omnimarket.delegate-skill-failed.v1",)
    )


@pytest.mark.unit
def test_compile_only_does_not_publish() -> None:
    adapter = DelegationDispatchAdapter()
    result = adapter.compile_request(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    assert result["command_topic"] == "onex.cmd.omnimarket.delegate-skill.v1"
    assert result["terminal_events"]["success"] == (
        "onex.evt.omnimarket.delegate-skill-completed.v1"
    )
    assert result["terminal_events"]["failure"] == (
        "onex.evt.omnimarket.delegate-skill-failed.v1"
    )
    assert "correlation_id" in result
    UUID(str(result["correlation_id"]))
    assert result["payload"]["task_type"] == "test"


@pytest.mark.unit
def test_compile_only_uses_provided_correlation_id() -> None:
    cid = "11111111-1111-1111-1111-111111111111"
    adapter = DelegationDispatchAdapter()
    result = adapter.compile_request(
        prompt="Test",
        task_type="test",
        source="claude-code",
        correlation_id=cid,
    )
    assert str(result["correlation_id"]) == cid


@pytest.mark.unit
def test_cli_compile_only_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    rc = main(
        [
            "--prompt",
            "Write a unit test",
            "--task-type",
            "test",
            "--source",
            "claude-code",
            "--compile-only",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["command_topic"] == "onex.cmd.omnimarket.delegate-skill.v1"


@pytest.mark.unit
def test_cli_rejects_non_uuid_correlation_id() -> None:
    rc = main(
        [
            "--prompt",
            "Test",
            "--task-type",
            "test",
            "--source",
            "claude-code",
            "--correlation-id",
            "not-a-uuid",
            "--compile-only",
        ]
    )
    assert rc != 0


@pytest.mark.unit
def test_cli_rejects_invalid_task_type() -> None:
    rc = main(
        [
            "--prompt",
            "Test",
            "--task-type",
            "invalid",
            "--source",
            "claude-code",
            "--compile-only",
        ]
    )
    assert rc != 0
