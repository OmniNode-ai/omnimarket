# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: OpenCode Go as a provider, with what its docs ask of every client.

Operator 2026-11-26: "add both" (OpenCode and OpenCode Go). opencode.ai/docs/go
(read 2026-11-26), "Where can I use it?": a client should "Identify itself with
its own user agent, such as my-coding-agent/1.0, rather than a generic SDK or
HTTP-library name" and "Send a stable session ID in x-opencode-session for each
conversation"; the Console harnesses page shows it as ``x-opencode-session:
ses_...`` and says Go requires it. A key with no Go subscription is refused
("OpenCode Go subscription required.").

One delegation run is one conversation: its correlation id is the same across
the run's retries and new for the next run.

The fixture is four entries of Go's real answer at
https://opencode.ai/inference/go/openai/v1/models, recorded 2026-11-26.

Failure modes, each with a test:
  G1 /config does not offer Go, under its name, before the Local Model.
  G2 a Go request leaves out onex's own user agent or the session header.
  G3 the session id changes inside one run, or repeats across runs.
  G4 another provider's request is sent those headers.
  G5 Go's live list is not read from its own address with onex's user agent,
     or offers a free-tier or non-chat model.
  G6 the key window does not say a Go subscription is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from textual.widgets import Input

from omnimarket.handlers.routing.byok_model_listing import list_byok_models
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import KeyScreen
from tests.unit.nodes.node_onex_setup_effect.test_config_providers_table import (
    _box_text,
    _config,
    _go_to,
    _make,
    _runner,
    _settle,
)

_GO_CHAT = "https://opencode.ai/inference/go/openai/v1/chat/completions"
_OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
_RECORDED = json.loads(
    (
        Path(__file__).parent / "fixtures" / "opencode_go_models_2026-11-26.json"
    ).read_text(encoding="utf-8")
)


def _headers(endpoint: str, correlation_id: str) -> dict[str, str]:
    request = ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=correlation_id,
        causation_id=str(uuid4()),
        model_id="deepseek-v4.1-flash",
        endpoint_ref=endpoint,
        prompt="p",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        secret_ref=None,
        api_key_env=None,
    )
    headers, _ = HandlerLlmDelegationCall._resolve_outbound_headers(request)
    return headers


def test_config_offers_opencode_go_before_the_local_model() -> None:
    """G1."""
    labels = {e.provider: e.label for e in cli_tui._picker()}
    assert labels.get("opencode-go") == "OpenCode Go"
    assert cli_tui._hosted("opencode-go")
    assert [e.provider for e in cli_tui._picker()][-1] == "local"


def test_a_go_request_names_onex_and_its_conversation() -> None:
    """G2."""
    run = str(uuid4())
    headers = _headers(_GO_CHAT, run)
    agent = headers.get("User-Agent", "")
    assert agent.startswith("onex/"), headers
    assert "python" not in agent.lower()
    assert "httpx" not in agent.lower()
    assert headers.get("x-opencode-session") == "ses_" + run.replace("-", "")


def test_the_session_holds_for_a_run_and_changes_for_the_next() -> None:
    """G3."""
    run = str(uuid4())
    first = _headers(_GO_CHAT, run)["x-opencode-session"]
    retry = _headers(_GO_CHAT, run)["x-opencode-session"]
    later = _headers(_GO_CHAT, str(uuid4()))["x-opencode-session"]
    assert first == retry
    assert later != first


def test_another_providers_request_is_left_alone() -> None:
    """G4."""
    headers = _headers(_OPENROUTER_CHAT, str(uuid4()))
    assert "x-opencode-session" not in headers
    assert "User-Agent" not in headers
    assert "x-opencode-session" in _headers(_GO_CHAT, str(uuid4()))  # the control


def test_gos_own_list_is_read_live_with_onexs_agent() -> None:
    """G5."""
    seen: list[dict[str, Any]] = []

    def get(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return _RECORDED

    listing = list_byok_models("opencode-go", "fixture-key", get=get)
    assert [call["url"] for call in seen] == [
        "https://opencode.ai/inference/go/openai/v1/models"
    ]
    sent = seen[0]["extra_headers"]
    assert sent.get("Authorization") == "Bearer fixture-key"
    assert sent.get("User-Agent", "").startswith("onex/")
    assert listing.outcome == "listed"
    models = {m.id: m for m in listing.models}
    assert sorted(models) == ["claude-haiku-5-5", "deepseek-v4.1-flash"]
    flash = models["deepseek-v4.1-flash"]
    assert flash.name == "DeepSeek V4.1 Flash 1017"
    assert flash.context_tokens == 1_000_000
    assert flash.input_usd_per_million == 0.15
    assert flash.output_usd_per_million == 0.6
    assert flash.input_modalities == ("text", "image")


async def test_its_key_window_says_a_go_subscription_is_needed() -> None:
    """G6."""
    runner = _runner()
    app, _ = _make(runner)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await _config(pilot, app)
        await _go_to(pilot, screen, "opencode-go")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, KeyScreen)
        assert "OpenCode Go" in str(app.screen.query_one("#ks-box").border_title)
        assert app.focused is app.screen.query_one("#ks-field", Input)
        said = _box_text(app.screen.query_one("#ks-box"))
        assert "opencode.ai/console" in said, said
        assert "Go subscription" in said, said
