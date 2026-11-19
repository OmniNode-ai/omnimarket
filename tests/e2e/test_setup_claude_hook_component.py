# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered but disabled hooks must not satisfy setup verification.

Uses the installed ONEX settings and mask commands in an isolated HOME. No
provider credentials, model requests, or real user settings are involved.
"""

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
    HandlerSetupClaude,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.e2e


@pytest.mark.release_proof
def test_hook_component_verifies_the_effective_mask_and_preserves_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook component verifies through the omninode-claude customer bundle,
    # the same packaged dependency as test_setup_installed_legacy_hooks.
    if not metadata.entry_points(group="onex.claude", name="customer_assets"):
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("Hook component journey requires the omninode-claude bundle")
        pytest.skip("Hook component journey requires the omninode-claude bundle")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("OMNIBASE_ENV_FILE", str(home / ".omnibase" / ".env"))
    monkeypatch.delenv("ONEX_HOOKS_MASK", raising=False)
    monkeypatch.delenv("ONEX_CONFIG_PATH", raising=False)
    binary = str(Path(sys.executable).parent / "onex")

    def run(*args: str) -> None:
        result = subprocess.run(
            [binary, *args],
            cwd=tmp_path,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    # OMN-23413: install through setup's own repair path, as /delegation's Save
    # does: the delegation hook closure (install-delegation.sh), then the
    # switch.
    # install-delegation.sh requires a Claude Code CLI on PATH; a stub answers.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "claude").write_text("#!/bin/sh\necho '2.1.323 (Claude Code)'\n")
    (tools / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    plan = ModelSetupPlan(
        workspace=tmp_path / "workspace",
        action="repair",
        components=("claude-plugin", "claude-hooks"),
    )
    component = HandlerSetupClaude(name="claude-hooks")
    assert component.apply(plan) is None
    assert component.observe(plan).status == "success"
    settings = home / ".claude" / "settings.json"
    original = settings.read_bytes()

    run("hooks", "disable", "POST_SKILL_DELEGATION_ENFORCER")
    observed = component.observe(plan)
    assert observed.status != "success", "disabled hooks were reported ready"
    assert "POST_SKILL_DELEGATION_ENFORCER" in (observed.fix or "")
    assert settings.read_bytes() == original

    # Environment precedence is effective for the newly launched Claude process.
    monkeypatch.setenv("ONEX_HOOKS_MASK", "0x800")
    assert component.observe(plan).status == "success"
    monkeypatch.setenv("ONEX_HOOKS_MASK", "0")
    assert component.observe(plan).status != "success"
    monkeypatch.delenv("ONEX_HOOKS_MASK")
    run("hooks", "enable", "POST_SKILL_DELEGATION_ENFORCER")
    assert component.observe(plan).status == "success"
    assert settings.read_bytes() == original


# OMN-23413, found 2026-11-13: the hooks check compared the installed hooks with
# this onex's packaged copy, so a hook an older onex installed, untouched, read as
# "changed after installation" and Save refused for good on every upgrade (and
# when onex moved, since the hook rows name its Python). Failure modes:
#   U1  an untouched older install is refused instead of updated.
#   U2  an install the user edited is overwritten without being asked.
#   U3  the way out (--replace) loses the user's edits, or does not reinstall.


def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelSetupPlan:
    if not metadata.entry_points(group="onex.claude", name="customer_assets"):
        pytest.skip("Hook upgrade journey requires the omninode-claude bundle")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("OMNIBASE_ENV_FILE", str(home / ".omnibase" / ".env"))
    monkeypatch.delenv("ONEX_HOOKS_MASK", raising=False)
    monkeypatch.delenv("ONEX_CONFIG_PATH", raising=False)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "claude").write_text("#!/bin/sh\necho '2.1.323 (Claude Code)'\n")
    (tools / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    plan = ModelSetupPlan(
        workspace=tmp_path / "workspace",
        action="repair",
        components=("claude-plugin", "claude-hooks"),
    )
    assert HandlerSetupClaude(name="claude-hooks").apply(plan) is None
    return plan


def _runner(home: Path) -> Path:
    return (
        home
        / ".omninode/delegation/plugin/plugins/onex/hooks/lib/delegation_hook_runner.py"
    )


def _older(home: Path) -> None:
    """What an older onex left: other hook bytes, rows naming another Python."""
    runner = _runner(home)
    runner.write_text(runner.read_text() + "# an older release\n")
    for path in (
        home / ".claude" / "settings.json",
        home / ".omninode" / "delegation" / "hooks-delegation.json",
    ):
        path.write_text(
            path.read_text().replace(
                f"PLUGIN_PYTHON_BIN={sys.executable}",
                "PLUGIN_PYTHON_BIN=/old/onex/python",
            )
        )


@pytest.mark.release_proof
def test_an_untouched_older_install_is_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1."""
    from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
        record_installed_hooks,
    )

    plan = _isolated(tmp_path, monkeypatch)
    home = tmp_path / "home"
    _older(home)
    record_installed_hooks(home / ".claude")  # the record that older onex wrote
    component = HandlerSetupClaude(name="claude-hooks")
    seen = component.observe(plan)
    assert seen.status == "unavailable", seen
    assert "older" in (seen.fix or "")
    assert component.apply(plan) is None
    assert component.observe(plan).status == "success"
    assert "an older release" not in _runner(home).read_text()
    assert "/old/onex/python" not in (home / ".claude" / "settings.json").read_text()


@pytest.mark.release_proof
def test_an_edited_install_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U2: no record matches (edited, or installed before records existed)."""
    plan = _isolated(tmp_path, monkeypatch)
    home = tmp_path / "home"
    runner = _runner(home)
    runner.write_text(runner.read_text() + "# my own change\n")
    component = HandlerSetupClaude(name="claude-hooks")
    seen = component.observe(plan)
    assert seen.status == "action_required"
    assert "--replace" in (seen.fix or "")
    assert component.apply(plan) == seen
    assert "# my own change" in runner.read_text()


@pytest.mark.release_proof
def test_replace_keeps_a_copy_then_reinstalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U3."""
    plan = _isolated(tmp_path, monkeypatch)
    home = tmp_path / "home"
    runner = _runner(home)
    runner.write_text(runner.read_text() + "# my own change\n")
    component = HandlerSetupClaude(name="claude-hooks", replace=True)
    assert component.apply(plan) is None
    assert component.observe(plan).status == "success"
    assert "# my own change" not in runner.read_text()
    kept = list((home / ".omninode" / "delegation" / "backups").rglob(runner.name))
    assert len(kept) == 1
    assert "# my own change" in kept[0].read_text()
