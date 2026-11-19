# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegation on --no-prompts`: the prompt hook's switch (OMN-23413).

Operator, 2026-11-15: "build separate switches"; operator, 2026-11-17
(Amendment 14a): one hook, the prompt hook. It stays installed;
~/.onex/delegation.json says whether it delegates, so a pause never edits
Claude Code's settings.

Failure modes:
  X1  `on --no-prompts` does not save the hook paused, or reinstalls hooks that
      are already in place.
  X2  plain `on` keeps the hook paused from before.
  X4  status does not say whether the hook delegates or is paused.
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


def test_on_no_prompts_saves_the_hook_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X1, X4."""
    claude = _ready()
    code, doc, switched = _run(monkeypatch, tmp_path, claude, "on", "--no-prompts")
    assert code == 0
    assert doc["status"] == "on"
    assert read_switches().prompts is False
    assert doc["switches"] == {"prompts": "paused"}
    assert "claude-hooks" not in claude.applied  # installed already: nothing rewritten
    assert switched == [("on",)]


def test_plain_on_turns_the_hook_back_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X2."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_switches(ModelDelegationSwitches(prompts=False))
    code, doc, _switched = _run(monkeypatch, tmp_path, _ready(), "on")
    assert code == 0
    assert read_switches().prompts is True
    assert doc["switches"] == {"prompts": "on"}


def test_status_names_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X4: JSON and text."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_switches(ModelDelegationSwitches(prompts=True))
    _code, doc, _switched = _run(monkeypatch, tmp_path, _ready(), "status")
    assert doc["switches"] == {"prompts": "on"}
    from click.testing import CliRunner

    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", _ready().make)
    text = CliRunner().invoke(cli_delegation.delegation_group, ["status"]).output
    assert "Prompts: on" in text
    assert json.loads(json.dumps(doc))["status"] == "on"
