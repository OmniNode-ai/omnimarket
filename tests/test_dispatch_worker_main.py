# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_dispatch_worker __main__.py CLI.

Tests that `python -m omnimarket.nodes.node_dispatch_worker` is invocable and
exits with the expected codes for valid inputs and error cases.

These tests use subprocess.run via `uv run python -m ...` to verify the actual
CLI entry point works end-to-end — this is the exact invocation used by the
overseer anti-passivity tick.

Related: OMN-9438
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# Worktree root for uv run invocations (set to the package root, not omni_home)
_WORKTREE_ROOT = Path(__file__).parent.parent


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `uv run python -m omnimarket.nodes.node_dispatch_worker <args>`."""
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "omnimarket.nodes.node_dispatch_worker",
        *args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or _WORKTREE_ROOT),
    )


@pytest.mark.unit
def test_ticket_dry_run_exits_zero() -> None:
    """--ticket OMN-0000 --dry-run should exit 0 with JSON output."""
    result = _run(["--ticket", "OMN-0000", "--dry-run"])
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["name"] == "omn-0000-fixer"
    assert payload["role"] == "fixer"
    assert payload["team"] == "Omninode"
    assert "OMN-0000" in payload["targets"]


@pytest.mark.unit
def test_full_form_dry_run_exits_zero() -> None:
    """Full explicit args with --dry-run should exit 0."""
    result = _run(
        [
            "--name",
            "test-fixer",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "Fix X",
            "--targets",
            "OMN-9438",
            "--dry-run",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["name"] == "test-fixer"
    assert payload["role"] == "fixer"
    assert payload["scope"] == "Fix X"
    assert "OMN-9438" in payload["targets"]


@pytest.mark.unit
def test_ticket_json_output_exits_zero() -> None:
    """--ticket OMN-0000 --json should exit 0 with structured JSON result.

    The fixer role requires both a ticket (OMN-XXXX) and a repo (repo#PR) in
    targets so the handler can derive the worktree path. We supply both.
    """
    result = _run(
        [
            "--ticket",
            "OMN-0000",
            "--targets",
            "omnimarket#1",
            "--json",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    # ModelDispatchWorkerResult fields
    assert "validated_task_description" in payload
    assert "validated_prompt_template" in payload
    assert "proposed_agent_spawn_args" in payload
    assert "collision_fence_embeds" in payload


@pytest.mark.unit
def test_ticket_human_output_exits_zero() -> None:
    """--ticket OMN-0000 (human mode) should exit 0 and print readable output.

    Fixer role needs a repo#PR target in addition to the ticket; supply one.
    """
    result = _run(["--ticket", "OMN-0000", "--targets", "omnimarket#1"])
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "Dispatch Worker Compilation" in result.stdout
    assert "Status: OK" in result.stdout


@pytest.mark.unit
def test_missing_required_args_exits_nonzero() -> None:
    """Invoking with no args should exit non-zero (argparse error)."""
    result = _run([])
    assert result.returncode != 0


@pytest.mark.unit
def test_invalid_role_exits_nonzero() -> None:
    """--role with an invalid value should fail."""
    result = _run(
        [
            "--name",
            "test-worker",
            "--team",
            "Omninode",
            "--role",
            "invalid_role",
            "--scope",
            "Test scope",
            "--targets",
            "OMN-9438",
        ]
    )
    assert result.returncode != 0


@pytest.mark.unit
def test_watcher_role_dry_run() -> None:
    """Watcher role requires a PR target; dry-run should still exit 0."""
    result = _run(
        [
            "--name",
            "test-watcher",
            "--team",
            "Omninode",
            "--role",
            "watcher",
            "--scope",
            "Watch PR 42",
            "--targets",
            "omnimarket#42",
            "--dry-run",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["role"] == "watcher"


@pytest.mark.unit
def test_collision_fences_passed_through() -> None:
    """--collision-fences values appear in the dry-run payload."""
    result = _run(
        [
            "--ticket",
            "OMN-0001",
            "--collision-fences",
            "OMN-9999",
            "--collision-fences",
            "omnimarket#200",
            "--dry-run",
        ]
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "OMN-9999" in payload["collision_fences"]
    assert "omnimarket#200" in payload["collision_fences"]


@pytest.mark.unit
def test_replace_flag_passed_through() -> None:
    """--replace flag appears as True in the dry-run payload."""
    result = _run(["--ticket", "OMN-0002", "--replace", "--dry-run"])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["replace"] is True


@pytest.mark.unit
def test_module_is_invocable_as_module() -> None:
    """Verify the -m invocation path resolves correctly (smoke test)."""
    # --dry-run is the lightest possible invocation
    result = _run(["--ticket", "OMN-9438", "--dry-run"])
    assert result.returncode == 0, (
        f"Module invocation failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


@pytest.mark.unit
def test_dry_run_validates_model_constraints() -> None:
    """--dry-run must reject inputs that violate ModelDispatchWorkerCommand constraints.

    wall_clock_cap_min=999 exceeds the max of 480; previously dry-run would
    exit 0 with unvalidated argparse values.  Now the model is constructed
    first, so invalid values are caught before printing.
    """
    result = _run(
        [
            "--name",
            "test-fixer",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "Test scope",
            "--targets",
            "OMN-9438",
            "--wall-clock-cap-min",
            "999",
            "--dry-run",
        ]
    )
    assert result.returncode != 0, (
        "Expected non-zero exit for out-of-bounds wall_clock_cap_min in --dry-run, "
        f"got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Validation error" in result.stderr
