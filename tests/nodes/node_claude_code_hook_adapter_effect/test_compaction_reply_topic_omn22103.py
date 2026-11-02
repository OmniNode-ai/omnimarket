# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction binding awaits per-principal reply topics and names them on the command (OMN-22103).

The shared reply topics let any developer principal read another machine's
compaction package. The binding now awaits topics templated on the requester's
principal, and the command on the shared command topic carries the three
resolved names so the orchestrator replies to exactly those.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import JsonValue

from omnimarket.models.context_compaction import ModelContextCompactionRequested
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers import (
    reply_principal,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    HandlerClaudeCodeHookAdapter,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    CONTRACT_PATH,
    load_client_section,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.reply_principal import (
    ReplyPrincipalResolver,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelHookAdapterRequest,
    ModelReceiptEvent,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.runtime_claude_code_hook_cli import (
    main,
)

pytestmark = pytest.mark.unit

PRINCIPAL = "dev-test-principal"
INVOCATION = UUID("00000000-0000-4000-8000-000000000022")
PACKAGE_TOPIC = (
    f"onex.cmd.omniintelligence.context-compaction-package-for-{PRINCIPAL}.v1"
)
PREPARED_TOPIC = f"onex.evt.omnimarket.context-compaction-prepared-for-{PRINCIPAL}.v1"
FALLBACK_TOPIC = f"onex.evt.omnimarket.context-compaction-fallback-for-{PRINCIPAL}.v1"
RESOLVED = (PACKAGE_TOPIC, PREPARED_TOPIC, FALLBACK_TOPIC)
HOOK_INPUT: dict[str, Any] = {
    "session_id": "session-22103",
    "cwd": "/work/repo",
    "trigger": "manual",
    "messages": [{"role": "user", "text": "compact me"}],
}


class _Journal:
    def journal(self, session_id: str) -> str:
        return "barrier-22103"


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish_and_await(
        self,
        *,
        topic: str,
        command_type: str,
        payload: dict[str, JsonValue],
        awaited_events: tuple[str, ...],
        deadline_ms: int,
    ) -> tuple[ModelReceiptEvent, ...]:
        self.calls.append(
            {"topic": topic, "payload": payload, "awaited_events": awaited_events}
        )
        return ()


def _binding() -> Any:
    section = load_client_section(CONTRACT_PATH)
    return next(b for b in section.bindings if b.name == "compaction")


def _adapter(client: _RecordingClient, resolver: Any) -> HandlerClaudeCodeHookAdapter:
    return HandlerClaudeCodeHookAdapter(
        client,
        invocation_ids=lambda: INVOCATION,
        barrier_journal=_Journal(),
        principal_resolver=resolver,
    )


def test_compaction_reply_topic_binding_awaits_three_principal_templated_topics() -> (
    None
):
    binding = _binding()
    assert len(binding.awaited_events) == 3
    assert all(t.count("{principal}") == 1 for t in binding.awaited_events)
    assert binding.command_topic == (
        "onex.cmd.omniintelligence.context-compaction-requested.v1"
    )
    sources = {a.name: a.source for a in binding.arguments}
    assert sources["reply_topic_package"] == "@awaited_event:0"
    assert sources["reply_topic_prepared"] == "@awaited_event:1"
    assert sources["reply_topic_fallback"] == "@awaited_event:2"


def test_compaction_reply_topic_command_carries_the_three_resolved_names() -> None:
    client = _RecordingClient()
    _adapter(client, lambda: PRINCIPAL).handle(
        ModelHookAdapterRequest(binding=_binding(), hook_input=HOOK_INPUT)
    )
    (call,) = client.calls
    assert call["topic"] == "onex.cmd.omniintelligence.context-compaction-requested.v1"
    assert call["awaited_events"] == RESOLVED
    payload = call["payload"]
    assert payload["reply_topic_package"] == PACKAGE_TOPIC
    assert payload["reply_topic_prepared"] == PREPARED_TOPIC
    assert payload["reply_topic_fallback"] == FALLBACK_TOPIC
    assert "{principal}" not in json.dumps(payload)
    command = ModelContextCompactionRequested.model_validate(payload)
    assert command.invocation_id == INVOCATION


@pytest.mark.parametrize(
    "bad",
    ["", "Upper", "has space", "-leading", "a" * 64, "a.b", "a/b", "{principal}"],
)
def test_compaction_reply_topic_invalid_principal_is_refused_before_publishing(
    bad: str,
) -> None:
    client = _RecordingClient()
    with pytest.raises(ValueError, match="principal"):
        _adapter(client, lambda: bad).handle(
            ModelHookAdapterRequest(binding=_binding(), hook_input=HOOK_INPUT)
        )
    assert client.calls == []


@pytest.mark.parametrize("good", ["a", "dev-test-principal", "m1-2", "a" * 63])
def test_compaction_reply_topic_valid_principal_is_published(good: str) -> None:
    client = _RecordingClient()
    _adapter(client, lambda: good).handle(
        ModelHookAdapterRequest(binding=_binding(), hook_input=HOOK_INPUT)
    )
    (call,) = client.calls
    assert all(good in t for t in call["awaited_events"])


def test_compaction_reply_topic_binding_without_a_resolver_refuses_to_run() -> None:
    client = _RecordingClient()
    with pytest.raises(RuntimeError, match="principal"):
        _adapter(client, None).handle(
            ModelHookAdapterRequest(binding=_binding(), hook_input=HOOK_INPUT)
        )
    assert client.calls == []


def test_compaction_reply_topic_cli_main_resolves_the_principal_through_the_injected_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import io

    monkeypatch.setenv("ONEX_HOOK_EMIT_JOURNAL_DIR", str(tmp_path / "journal"))
    client = _RecordingClient()
    err = io.StringIO()
    code = main(
        ["--binding", "compaction"],
        client=client,
        principal_resolver=lambda: PRINCIPAL,
        stdin=io.StringIO(json.dumps(HOOK_INPUT)),
        stdout=io.StringIO(),
        stderr=err,
    )
    assert code == 0, err.getvalue()
    (call,) = client.calls
    assert call["awaited_events"] == RESOLVED
    assert call["payload"]["reply_topic_package"] == PACKAGE_TOPIC


class _Transport:
    def __init__(self, sasl_username: str | None) -> None:
        self.sasl_username = sasl_username


def test_compaction_reply_topic_production_resolver_returns_the_lane_sasl_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes: list[str] = []

    def resolve(lane: str) -> tuple[object, object]:
        lanes.append(lane)
        return object(), _Transport(PRINCIPAL)

    monkeypatch.setattr(reply_principal, "resolve_lane_binding", resolve)
    assert ReplyPrincipalResolver().resolve() == PRINCIPAL
    assert lanes == ["dev"]


@pytest.mark.parametrize("transport", [None, _Transport(None), _Transport("")])
def test_compaction_reply_topic_production_resolver_refuses_without_a_username(
    monkeypatch: pytest.MonkeyPatch, transport: object
) -> None:
    monkeypatch.setattr(
        reply_principal,
        "resolve_lane_binding",
        lambda _lane: (object(), transport),
    )
    with pytest.raises(RuntimeError, match="principal"):
        ReplyPrincipalResolver().resolve()


def test_compaction_reply_topic_adapter_contract_matches_the_orchestrator_templates() -> (
    None
):
    from omnimarket.models.context_compaction import load_reply_topic_templates

    templates = load_reply_topic_templates()
    expected = tuple(
        t.template for t in (templates.package, templates.prepared, templates.fallback)
    )
    assert tuple(_binding().awaited_events) == expected
    assert Path(CONTRACT_PATH).is_file()
