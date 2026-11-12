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

    run("claude", "hooks", "install", "--yes", "--json")
    run("hooks", "enable", "POST_SKILL_DELEGATION_ENFORCER")
    plan = ModelSetupPlan(
        workspace=tmp_path / "workspace",
        action="verify",
        components=("claude-plugin", "claude-hooks"),
    )
    component = HandlerSetupClaude(name="claude-hooks")
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
