# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/validation/run_dep_health_gate.sh pre-commit hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
HOOK_SCRIPT = REPO_ROOT / "scripts" / "validation" / "run_dep_health_gate.sh"


@pytest.mark.unit
class TestDepHealthPrecommitHook:
    """Tests for the dep-health pre-commit gate shell script."""

    def test_hook_script_exists(self) -> None:
        """The hook script must exist at the expected path."""
        assert HOOK_SCRIPT.exists(), f"Hook script not found: {HOOK_SCRIPT}"

    def test_hook_script_is_executable_or_bash_invocable(self) -> None:
        """The hook script is invocable via bash."""
        result = subprocess.run(
            ["bash", "--norc", "-n", str(HOOK_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax check failed: {result.stderr}"

    def test_hook_uses_set_euo_pipefail(self) -> None:
        """The hook script must use set -euo pipefail for safety."""
        content = HOOK_SCRIPT.read_text()
        assert "set -euo pipefail" in content, (
            "Hook script must start with 'set -euo pipefail'"
        )

    def test_hook_invokes_ci_script(self) -> None:
        """The hook must delegate to run_dep_health_sweep.py."""
        content = HOOK_SCRIPT.read_text()
        assert "run_dep_health_sweep.py" in content, (
            "Hook must invoke scripts/ci/run_dep_health_sweep.py"
        )

    def test_hook_uses_exit_nonzero_flag(self) -> None:
        """The hook must use --exit-nonzero-on-findings for blocking enforcement."""
        content = HOOK_SCRIPT.read_text()
        assert "--exit-nonzero-on-findings" in content, (
            "Hook must pass --exit-nonzero-on-findings to the CI script"
        )

    def test_no_hardcoded_absolute_paths_in_hook(self) -> None:
        """Hook must not contain hardcoded /Users/ or /Volumes/ paths."""
        content = HOOK_SCRIPT.read_text()
        assert "/Users/" not in content, "Hook must not hardcode /Users/ paths"
        assert "/Volumes/" not in content, "Hook must not hardcode /Volumes/ paths"

    def test_precommit_config_contains_dep_health_gate(self) -> None:
        """The .pre-commit-config.yaml must declare the dep-health-gate hook."""
        config_path = REPO_ROOT / ".pre-commit-config.yaml"
        assert config_path.exists(), ".pre-commit-config.yaml not found"
        content = config_path.read_text()
        assert "dep-health-gate" in content, (
            ".pre-commit-config.yaml must define the dep-health-gate hook"
        )

    def test_precommit_config_hook_uses_language_system(self) -> None:
        """The dep-health-gate hook must use language: system."""
        config_path = REPO_ROOT / ".pre-commit-config.yaml"
        content = config_path.read_text()
        # Find the dep-health-gate block and check it uses language: system
        hook_start = content.find("dep-health-gate")
        assert hook_start >= 0
        # Check within a reasonable window after the hook id
        hook_block = content[hook_start : hook_start + 400]
        assert "language: system" in hook_block, (
            "dep-health-gate hook must use 'language: system'"
        )

    def test_precommit_config_hook_pass_filenames_false(self) -> None:
        """The dep-health-gate hook must use pass_filenames: false."""
        config_path = REPO_ROOT / ".pre-commit-config.yaml"
        content = config_path.read_text()
        hook_start = content.find("dep-health-gate")
        assert hook_start >= 0
        hook_block = content[hook_start : hook_start + 400]
        assert "pass_filenames: false" in hook_block, (
            "dep-health-gate hook must use 'pass_filenames: false'"
        )
