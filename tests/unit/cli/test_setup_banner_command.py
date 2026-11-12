# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The welcome screen precedes prompts, including an interrupted first run."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli as setup_cli

pytestmark = pytest.mark.unit


def test_welcome_precedes_first_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        setup_cli, "user_config_path", lambda: tmp_path / ".onex/config.yaml"
    )
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    # From the home folder setup asks for the workspace (AC19); elsewhere the
    # current folder is the workspace and there is no workspace prompt.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(setup_cli.setup_command, ["--interactive"], input="")
    title = "Welcome To OmniNode Delegation Onboarding"
    assert title in result.output
    assert result.output.index(title) < result.output.index("Workspace root")
    assert not (tmp_path / ".onex").exists()


def test_help_is_available_without_starting_onboarding() -> None:
    result = CliRunner().invoke(setup_cli.setup_command, ["--help"])
    assert result.exit_code == 0
    assert "--workspace" in result.output
    assert "Welcome To OmniNode Delegation Onboarding" not in result.output
