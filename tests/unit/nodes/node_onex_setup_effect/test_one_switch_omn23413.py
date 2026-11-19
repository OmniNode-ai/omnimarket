# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test that Claude Code delegation is one hook with one switch (OMN-23413 AC39, AC40b).

The bundle registers only the UserPromptSubmit prompt hook and its history
helper; subagents always run on Claude (operator 2026-11-17, Amendment 14a).
The onex CLI `onex delegation on|status` and the TUI /delegation tab no longer
offer or report a subagent hook.

Failure modes:
  S1  A prompt-only bundle reads as missing a subagent hook, so
      `onex delegation on` refuses it and delegation never switches on.
  S2  Its install is not observed installed.
  S3  Observations still report subagent_hook.
  S4  `on` still offers --subagents.
  S5  status still reports Subagent tasks.
  S6  The tab still shows a subagent row.

Delegation: docstring drafted through `onex delegate` run
1ecdf460-eab3-49e1-98ee-8899da45c421.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from textual.widgets import Static

from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
    write_switches,
)
from omnimarket.nodes.node_hook_delegate_effect.models import ModelDelegationSwitches
from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
    missing_delegation_hooks,
)
from tests.unit.nodes.node_onex_setup_effect.test_cli_delegation import (
    _Claude,
    _obs,
    _run,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_delegation_hooks import (
    _app,
    _open,
)
from tests.unit.nodes.node_onex_setup_effect.test_setup_legacy_hooks import bundle

pytestmark = pytest.mark.unit

_PROMPT = "ONEX_DELEGATION_HOOK=prompts /x/hooks/scripts/pre_tool_use_delegation.sh"
_HELPER = (
    "/py /x/hooks/lib/delegation_hook_runner.py --history-only 2>/dev/null || true"
)


def _ready() -> _Claude:
    return _Claude(
        [_obs("claude-plugin", "success")], [_obs("claude-hooks", "success")]
    )


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: True)


def test_the_prompt_hook_alone_is_the_whole_delegation_hook_set() -> None:
    """S1: the one-hook template is complete; one without the prompt hook is not."""
    rows = {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": _PROMPT}]},
            {"hooks": [{"type": "command", "command": _HELPER}]},
        ]
    }
    assert missing_delegation_hooks(rows) == []
    assert missing_delegation_hooks({}) == ["prompt (UserPromptSubmit)"]


def test_a_one_hook_bundle_installs_and_reads_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1, S2, S3: observe, apply, observe again over the real observer."""
    owner, plan, _installed, _profile, _installer, commands = bundle(
        tmp_path, monkeypatch, drop=("PreToolUse", "PostToolUse")
    )
    before = owner.observe(plan)
    assert "bundle_missing" not in before.evidence
    assert owner.apply(plan) is None
    assert any("--apply" in argv for argv in commands)
    after = owner.observe(plan)
    assert after.status == "success", after
    assert after.evidence.get("prompt_hook") == "installed"
    assert "subagent_hook" not in after.evidence


def test_on_offers_no_subagent_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4."""
    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", _ready().make)
    usage = CliRunner().invoke(cli_delegation.delegation_group, ["on", "--help"])
    assert "subagent" not in usage.output.lower()
    refused = CliRunner().invoke(
        cli_delegation.delegation_group, ["on", "--no-subagents"]
    )
    assert refused.exit_code == 2  # click: no such option


def test_status_reports_one_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5: JSON and text."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_switches(ModelDelegationSwitches(prompts=False))
    _code, doc, _switched = _run(monkeypatch, tmp_path, _ready(), "status")
    assert doc["switches"] == {"prompts": "paused"}
    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", _ready().make)
    text = CliRunner().invoke(cli_delegation.delegation_group, ["status"]).output
    assert "Prompts: paused" in text
    assert "subagent" not in text.lower()


def test_off_removes_the_whole_hook_history_helper_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7 (AC43, Amendment 14d): off keeps the history helper.

    Verifies `onex delegation off` removes the whole delegation hook, the prompt
    hook and its history helper, so nothing records prompts while delegation is
    off; the delegation store keeps its run history (the installer's uninstall
    removes hook rows and files only). Delegation: drafted through `onex
    delegate` run b1a5754b-f702-428b-b76e-53c8e0265884.
    """
    claude = _ready()
    code, _doc, switched = _run(monkeypatch, tmp_path, claude, "off")
    assert code == 0
    assert switched == [("off",)]
    assert claude.removed == ["claude-hooks"]
    assert claude.preserve_history == [False]


def test_rollback_without_history_leaves_no_owned_row(tmp_path: Path) -> None:
    """S7 over the real rollback: the helper row goes with the prompt hook,
    and an unrelated hook stays."""
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
        HandlerSetupClaude,
    )

    root = tmp_path / "home" / ".omninode" / "delegation"
    prompt = f"ONEX_DELEGATION_HOOK=prompts {root}/plugin/plugins/onex{_PROMPT[2:]}"
    helper = f"/py {root}/plugin/plugins/onex/hooks/lib/delegation_hook_runner.py --history-only"
    unrelated = {"hooks": [{"type": "command", "command": "/usr/local/bin/mine.sh"}]}
    owned = {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": prompt}]},
            {"hooks": [{"type": "command", "command": helper}]},
        ]
    }
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    root.mkdir(parents=True)
    (root / "hooks-delegation.json").write_text(json.dumps({"hooks": owned}))
    settings.write_text(
        json.dumps({"model": "keep", "hooks": {**owned, "PreToolUse": [unrelated]}})
    )
    manifest = root / "rollback-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "claude_settings": str(settings),
                "installed_hook_config": str(root / "hooks-delegation.json"),
            }
        )
    )
    HandlerSetupClaude.rollback_hook_registration(manifest, preserve_history=False)
    after = json.loads(settings.read_text())
    commands = [
        hook["command"]
        for groups in (after.get("hooks") or {}).values()
        for group in groups
        for hook in group.get("hooks", [])
    ]
    assert commands == ["/usr/local/bin/mine.sh"]
    assert after["model"] == "keep"


async def test_the_delegation_tab_shows_one_hook() -> None:
    """S6."""
    words = tui_design().delegation
    assert [hook.key for hook in words.hooks] == ["prompt_hook"]
    status = ModelTuiStatus(
        models=1, delegation="hook", hooks={"prompt_hook": "installed"}
    )
    app = _app(status)
    async with app.run_test(size=(160, 46)) as pilot:
        await _open(pilot, app)
        rows = list(app.screen.query(".hook").results(Static))
        shown = " ".join(" ".join(str(row.render()).split()) for row in rows)
        title = " ".join(
            str(app.screen.query_one("#hooks-title", Static).render()).split()
        )
    assert len(rows) == 1
    assert "subagent" not in f"{shown} {title}".lower()
    assert "subagent" not in json.dumps(words.model_dump()).lower()
