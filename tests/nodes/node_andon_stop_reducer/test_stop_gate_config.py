# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class mode comes from the ``andon_stop`` overlay and the stopped engines from the supplied facts (OMN-21958).

The same stop refuses under ``enforce`` and admits under ``observe``; an absent overlay key refuses naming the key.
No handler of the three dispatching nodes holds a class mode or an engine name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract, stop_gate
from omnimarket.models.stop_gate import (
    EnumStopGateOutcome,
    EnumStopGateReason,
    EnumStopMode,
    ModelStopGateRequest,
    ModelStopScope,
)
from omnimarket.nodes.node_andon_stop_reducer.overlay import load_stop_policy
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    CLASS,
    NOW,
    STOPPED,
    policy,
    stop,
    write_overlay,
)

pytestmark = pytest.mark.unit

FSM = load_andon_stop_contract()[0]
SCOPE = ModelStopScope(kind="engine", key=STOPPED)
SRC = Path(__file__).resolve().parents[3] / "src/omnimarket"
HANDLERS = (
    SRC / "nodes/node_idle_slot_fill_effect/handlers/handler_idle_slot_fill.py",
    SRC / "nodes/node_remote_lane_compute/handlers/handler_remote_lane_placement.py",
    SRC
    / "nodes/node_landing_worker_engine_compute/handlers/handler_landing_worker_engine.py",
)


def decide(tmp_path: Path, mode: str) -> EnumStopGateOutcome:
    return stop_gate(
        ModelStopGateRequest(
            scope=SCOPE,
            stops=(stop(),),
            policy=policy(tmp_path, mode),
            now=NOW,
        ),
        FSM,
    ).outcome


def test_stop_gate_config_the_same_stop_refuses_under_enforce_and_admits_under_observe(
    tmp_path: Path,
) -> None:
    assert decide(tmp_path / "e", "enforce") is EnumStopGateOutcome.REFUSE
    assert decide(tmp_path / "o", "observe") is EnumStopGateOutcome.ADMIT


def test_stop_gate_config_the_overlay_file_decides_the_mode(tmp_path: Path) -> None:
    environ = write_overlay(tmp_path, {CLASS: "observe"})
    assert load_stop_policy(environ).modes[CLASS] is EnumStopMode.OBSERVE
    environ = write_overlay(tmp_path, {CLASS: "enforce"})
    assert load_stop_policy(environ).modes[CLASS] is EnumStopMode.ENFORCE


def test_stop_gate_config_an_absent_class_key_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    other = "ruling-drifted"
    loaded = load_stop_policy(write_overlay(tmp_path, {other: "observe"}))
    result = stop_gate(
        ModelStopGateRequest(scope=SCOPE, stops=(stop(),), policy=loaded, now=NOW),
        FSM,
    )
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert result.reason is EnumStopGateReason.MODE_MISSING
    assert f"andon_stop.modes.{CLASS}" in result.line


def test_stop_gate_config_an_absent_overlay_refuses_naming_the_keys(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=r"andon_stop\.modes"):
        load_stop_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_stop_gate_config_an_enforced_class_needs_its_citation(tmp_path: Path) -> None:
    folder = tmp_path / "andon_stop"
    folder.mkdir()
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump({"modes": {CLASS: "enforce"}, "enforce_citations": {}}),
        encoding="utf-8",
    )
    with pytest.raises(NodeOverlayError, match="enforce_citations"):
        load_stop_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_stop_gate_config_every_class_ships_in_observe_nowhere_enforced_here() -> None:
    """Section 5.3: no class is set to enforce by this change; the repository ships no overlay value."""
    shipped = list(SRC.rglob("andon_stop/overlay.yaml"))
    assert shipped == []


def test_stop_gate_config_no_dispatcher_holds_a_class_mode_literal() -> None:
    """The mode of a class is the overlay's. (The worker-engine node's own test also rules out an engine name.)"""
    for path in HANDLERS:
        text = path.read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        code = "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith("#")
        )
        literals = set(re.findall(r"[\"\']([A-Za-z0-9_\-]+)[\"\']", code))
        assert not ({"observe", "enforce"} & literals), path.name
        assert "EnumStopMode" not in code, path.name
