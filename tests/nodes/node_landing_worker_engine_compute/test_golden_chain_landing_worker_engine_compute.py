# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Engine routing assertions extracted from worker placement tests (OMN-17427).

Placement probes and host ranking belong to the caller. The injected placement
API supplies their outcomes so these tests exercise only the node's routing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from omnimarket.nodes.node_landing_worker_engine_compute import (
    HandlerLandingWorkerEngine,
)
from omnimarket.nodes.node_landing_worker_engine_compute.handlers.handler_landing_worker_engine import (
    CODEX_FALLBACK_RULE,
)
from omnimarket.nodes.node_landing_worker_engine_compute.models import (
    ModelLandingWorkerEngineRequest,
)

CLAUDE_HOSTS = ("host-a", "host-b", "host-c", "host-d")
CODEX_HOSTS = frozenset(CLAUDE_HOSTS[:3])
READINGS = tuple(SimpleNamespace(host=name) for name in CLAUDE_HOSTS)


def placement(
    *, claude: Any = None, codex: Any = None, reason: str = "LIMITED"
) -> Mock:
    api = Mock()
    api.CODEX_ENGINE = "codex_high"
    api.choose.return_value = claude
    api.choose_codex.return_value = codex
    api.resolve_engine.side_effect = lambda engine: engine
    api.preflight_skips.return_value = dict.fromkeys(CLAUDE_HOSTS, reason)
    return api


def request(api: Mock, **over: Any) -> ModelLandingWorkerEngineRequest:
    return ModelLandingWorkerEngineRequest(
        **{
            "readings": READINGS,
            "requested_engine": "claude_sonnet",
            "codex_first": False,
            "cause": False,
            "codex_allowed": CODEX_HOSTS,
            "env": {},
            "placement": api,
            **over,
        }
    )


def test_first_rung_codex_pick_preserves_the_record() -> None:
    pick = SimpleNamespace(engine="codex_high", host=SimpleNamespace(name="host-b"))
    api = placement(codex=pick)
    result = HandlerLandingWorkerEngine().handle(request(api, codex_first=True))
    assert result.pick is pick
    assert result.pick.engine == "codex_high"
    assert result.pick.host.name in CODEX_HOSTS
    assert result.record == {"codex_first": True}
    api.choose_codex.assert_called_once_with(list(READINGS), CODEX_HOSTS)
    api.choose.assert_not_called()


@pytest.mark.parametrize("reason", ["CODEX-LIMITED", "LOAD-OVER-BAR", "NO-CODEX-LOGIN"])
def test_no_codex_slot_falls_back_to_the_requested_claude_engine(reason: str) -> None:
    pick = SimpleNamespace(engine="claude_sonnet", host=SimpleNamespace(name="host-d"))
    api = placement(claude=pick, reason=reason)
    result = HandlerLandingWorkerEngine().handle(request(api, codex_first=True))
    assert result.pick is pick
    assert result.pick.engine == "claude_sonnet"
    assert result.record["codex_first"] is True
    assert all(reason in result.record["codex_fallback"][name] for name in CODEX_HOSTS)
    api.choose.assert_called_once_with("claude_sonnet", list(READINGS))
    api.choose_codex.assert_called_once()
    assert api.method_calls[:3] == [
        call.choose_codex(list(READINGS), CODEX_HOSTS),
        call.preflight_skips(list(READINGS), "codex_high", CODEX_HOSTS),
        call.choose("claude_sonnet", list(READINGS)),
    ]


def test_no_host_of_either_engine_has_room() -> None:
    api = placement(reason="LOAD-OVER-BAR")
    result = HandlerLandingWorkerEngine().handle(request(api, codex_first=True))
    assert result.pick is None
    assert all(
        "LOAD-OVER-BAR" in result.record["codex_fallback"][name] for name in CODEX_HOSTS
    )
    api.choose_codex.assert_called_once()


def test_escalation_uses_claude_where_it_has_headroom() -> None:
    pick = SimpleNamespace(engine="claude_opus", host=SimpleNamespace(name="host-c"))
    api = placement(claude=pick)
    result = HandlerLandingWorkerEngine().handle(
        request(api, requested_engine="claude_opus")
    )
    assert result.pick is pick
    assert result.pick.engine == "claude_opus"
    assert "codex_first" not in result.record
    assert "claude_no_headroom" not in result.record
    api.choose_codex.assert_not_called()


def test_claude_limited_pool_places_a_codex_worker() -> None:
    pick = SimpleNamespace(engine="codex_high", host=SimpleNamespace(name="host-b"))
    api = placement(codex=pick)
    result = HandlerLandingWorkerEngine().handle(
        request(api, requested_engine="claude_opus")
    )
    assert result.pick is pick
    assert result.pick.engine == "codex_high"
    assert result.pick.host.name in CODEX_HOSTS
    assert "codex_first" not in result.record
    assert all(
        "LIMITED" in result.record["claude_no_headroom"][name] for name in CLAUDE_HOSTS
    )
    assert api.method_calls == [
        call.choose("claude_opus", list(READINGS)),
        call.choose_codex(list(READINGS), CODEX_HOSTS),
        call.resolve_engine("claude_opus"),
        call.preflight_skips(list(READINGS), "claude_opus"),
    ]


def test_cause_brief_still_waits_for_claude() -> None:
    api = placement(codex=SimpleNamespace(engine="codex_high"))
    result = HandlerLandingWorkerEngine().handle(
        request(api, cause=True, requested_engine="claude_opus")
    )
    assert result.pick is None
    api.choose_codex.assert_not_called()


@pytest.mark.parametrize(
    "switch", ["LANDING_CODEX_FALLBACK", "LANDING_CODEX_MECHANICAL"]
)
def test_fallback_switch_turns_it_off(switch: str) -> None:
    api = placement(codex=SimpleNamespace(engine="codex_high"))
    result = HandlerLandingWorkerEngine().handle(
        request(api, requested_engine="claude_opus", env={switch: "0"})
    )
    assert result.pick is None
    api.choose_codex.assert_not_called()


def test_fallback_prompt_rule_keeps_the_source_instructions() -> None:
    prompt = CODEX_FALLBACK_RULE.format(engine="claude_opus", worktree="worktree")
    assert "you are the Codex engine" in prompt
    assert "no pool\nhost had Claude headroom" in prompt
    assert "claude_opus" in prompt
    assert "Never run with the sandbox off" in prompt
    assert "codex_attempt_failed" in prompt
    assert "nonmechanical_or_sandbox_refusal" not in prompt
    assert "first rung of three" not in prompt


def test_contract_default_handler_resolves_to_the_packaged_handler() -> None:
    import importlib
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_landing_worker_engine_compute as package

    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_landing_worker_engine_compute"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert "terminal_event" not in contract
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_class = getattr(importlib.import_module(module_name), class_name)
    assert callable(getattr(handler_class(), "handle", None))
