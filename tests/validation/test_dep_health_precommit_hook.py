# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/validation/run_dep_health_gate.sh pre-commit hook."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
HOOK_SCRIPT = REPO_ROOT / "scripts" / "validation" / "run_dep_health_gate.sh"
# OMN-16816: the shell hook is now a thin delegator. The sweep arguments, the
# content-addressed cache and the machine-wide scan lock live in this module.
GATE_WRAPPER = REPO_ROOT / "scripts" / "validation" / "dep_health_gate_cache.py"


def _load_gate_wrapper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "dep_health_gate_cache_hooktest", GATE_WRAPPER
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    def test_hook_delegates_to_the_cached_gate_wrapper(self) -> None:
        """The hook must hand off to the OMN-16816 cache/lock wrapper."""
        content = HOOK_SCRIPT.read_text()
        assert "dep_health_gate_cache.py" in content, (
            "Hook must invoke scripts/validation/dep_health_gate_cache.py"
        )

    def test_wrapper_invokes_ci_script(self) -> None:
        """The gate must still run scripts/ci/run_dep_health_sweep.py on a miss."""
        gate = _load_gate_wrapper()
        assert gate.SWEEP_RELPATH.as_posix() == "scripts/ci/run_dep_health_sweep.py"

    def test_gate_uses_delta_mode_when_a_baseline_is_present(self) -> None:
        """Delta-blocking is the committed posture — assert the real argv."""
        gate = _load_gate_wrapper()
        assert (REPO_ROOT / gate.BASELINE_RELPATH).is_file(), (
            "this repo commits .onex_state/dep_health_baseline.json (Phase 2)"
        )
        args = gate.build_sweep_args(REPO_ROOT)
        assert "--delta-mode" in args, "Gate must pass --delta-mode to the CI script"
        assert "--baseline-path" in args, "Gate must pass the baseline path"
        assert gate.BASELINE_RELPATH.as_posix() in args

    def test_gate_is_advisory_when_no_baseline_is_present(self, tmp_path: Path) -> None:
        """Phase 1 must not pass --delta-mode, which requires a baseline file."""
        gate = _load_gate_wrapper()
        args = gate.build_sweep_args(tmp_path)
        assert "--delta-mode" not in args
        assert "--baseline-path" not in args

    def test_no_hardcoded_absolute_paths_in_hook(self) -> None:
        """Hook must not contain hardcoded workstation path prefixes."""
        user_home_prefix = "/" + "Users" + "/"
        volume_prefix = "/" + "Volumes" + "/"
        content = HOOK_SCRIPT.read_text()
        assert user_home_prefix not in content, "Hook must not hardcode user paths"
        assert volume_prefix not in content, "Hook must not hardcode volume paths"

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
