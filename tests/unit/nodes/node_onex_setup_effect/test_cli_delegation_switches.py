# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegation on --no-prompts | --no-subagents`: one switch per hook (OMN-23413).

Operator, 2026-11-15: "how to enable and disable separate hooks?" then "build
separate switches". Both hooks stay installed; ~/.onex/delegation.json says
which delegates, so a pause never edits Claude Code's settings.

Failure modes:
  X1  `on --no-prompts` (or --no-subagents) does not save that hook paused and
      the other on, or reinstalls hooks that are already in place.
  X2  plain `on` keeps a hook paused from before.
  X3  both hooks paused is accepted as "on" instead of pointing to off.
  X4  status does not say, hook by hook, whether it delegates or is paused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    read_switches,
    write_switches,
)
from omnimarket.nodes.node_hook_delegate_effect.models import ModelDelegationSwitches
from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from tests.unit.nodes.node_onex_setup_effect.test_cli_delegation import (
    _Claude,
    _obs,
    _run,
)

pytestmark = pytest.mark.unit


def _ready() -> _Claude:
    return _Claude(
        [_obs("claude-plugin", "success")], [_obs("claude-hooks", "success")]
    )


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: True)


@pytest.mark.parametrize(
    ("flag", "prompts", "subagents"),
    [("--no-prompts", False, True), ("--no-subagents", True, False)],
)
def test_on_pauses_one_hook_and_keeps_the_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    prompts: bool,
    subagents: bool,
) -> None:
    """X1, X4."""
    claude = _ready()
    code, doc, switched = _run(monkeypatch, tmp_path, claude, "on", flag)
    assert code == 0
    assert doc["status"] == "on"
    assert read_switches() == ModelDelegationSwitches(
        prompts=prompts, subagents=subagents
    )
    assert doc["switches"] == {
        "prompts": "on" if prompts else "paused",
        "subagents": "on" if subagents else "paused",
    }
    assert "claude-hooks" not in claude.applied  # installed already: nothing rewritten
    assert switched == [("on",)]


def test_plain_on_turns_both_hooks_back_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X2."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_switches(ModelDelegationSwitches(prompts=False, subagents=True))
    code, doc, _switched = _run(monkeypatch, tmp_path, _ready(), "on")
    assert code == 0
    assert read_switches() == ModelDelegationSwitches(prompts=True, subagents=True)
    assert doc["switches"] == {"prompts": "on", "subagents": "on"}


def test_pausing_both_hooks_points_to_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X3."""
    claude = _ready()
    code, doc, switched = _run(
        monkeypatch, tmp_path, claude, "on", "--no-prompts", "--no-subagents"
    )
    assert code == 1
    assert "onex delegation off" in str(doc["fix"])
    assert claude.applied == []
    assert switched == []


def test_status_names_each_hooks_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X4: JSON and text."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_switches(ModelDelegationSwitches(prompts=True, subagents=False))
    _code, doc, _switched = _run(monkeypatch, tmp_path, _ready(), "status")
    assert doc["switches"] == {"prompts": "on", "subagents": "paused"}
    from click.testing import CliRunner

    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", _ready().make)
    text = CliRunner().invoke(cli_delegation.delegation_group, ["status"]).output
    assert "Prompts: on" in text
    assert "Subagent tasks: paused" in text
    assert json.loads(json.dumps(doc))["status"] == "on"
