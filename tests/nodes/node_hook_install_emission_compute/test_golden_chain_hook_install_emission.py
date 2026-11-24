# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the contract resolves and each install's three event kinds are read back (OMN-26658)."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallDeclared,
    ModelHookInstallEmissionRequest,
    ModelHookInstallObservedEvent,
    ModelHookInstallRequiredKind,
)

NAME = "node_hook_install_emission_compute"
T0 = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)

KINDS = (
    ModelHookInstallRequiredKind(
        kind="session_start", hook_event_names=("SessionStart",)
    ),
    ModelHookInstallRequiredKind(
        kind="tool_use", hook_event_names=("PreToolUse", "PostToolUse")
    ),
    ModelHookInstallRequiredKind(kind="stop", hook_event_names=("Stop",)),
)


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    entry = _contract()["handler_routing"]["handlers"][0]
    handler = entry["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def _event(
    host: str, actor: str, name: str, minutes: int
) -> ModelHookInstallObservedEvent:
    return ModelHookInstallObservedEvent(
        host=host,
        actor=actor,  # type: ignore[arg-type]
        hook_event_name=name,
        emitted_at=T0 + timedelta(minutes=minutes),
    )


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.hook-install-emission-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.hook-install-emission-decided.v1"
    ]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "judge_hook_install_emission"
    }


def test_golden_chain_claude_and_codex_installs_both_emit_all_three_kinds() -> None:
    request = ModelHookInstallEmissionRequest(
        installs=(
            ModelHookInstallDeclared(
                host="h053", tool="claude", host_aliases=("hp-gen9",)
            ),
            ModelHookInstallDeclared(
                host="h053", tool="codex", host_aliases=("hp-gen9",)
            ),
        ),
        required_kinds=KINDS,
        events=(
            _event("hp-gen9", "claude", "SessionStart", 1),
            _event("hp-gen9", "claude", "PostToolUse", 2),
            _event("HP-GEN9", "claude", "Stop", 3),
            _event("hp-gen9", "codex", "SessionStart", 4),
            _event("hp-gen9", "codex", "PreToolUse", 5),
            _event("hp-gen9", "codex", "Stop", 6),
        ),
        since=T0,
        read_at=T0 + timedelta(minutes=10),
    )
    result = _handler().handle(request)
    assert result.all_emitting is True
    assert [(v.host, v.tool, v.status) for v in result.installs] == [
        ("h053", "claude", "emitting"),
        ("h053", "codex", "emitting"),
    ]
    assert result.installs[0].newest_event_at == T0 + timedelta(minutes=3)
    assert result.installs[1].kinds_missing == ()
    assert result.undeclared == ()
