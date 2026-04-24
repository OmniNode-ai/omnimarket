# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_dispatch_worker __main__.py CLI.

Tests that `python -m omnimarket.nodes.node_dispatch_worker` is invocable and
exits with the expected codes for valid inputs and error cases.

These tests use subprocess.run via `uv run python -m ...` to verify the actual
CLI entry point works end-to-end — this is the exact invocation used by the
overseer anti-passivity tick.

Ported from the superseded PR #383; pruned to the portable subset that
exercises the CLI surface actually shipped in #389 (OMN-9438).

Related: OMN-9438
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_WORKTREE_ROOT = Path(__file__).parent.parent


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run `uv run python -m omnimarket.nodes.node_dispatch_worker <args>`.

    A 30-second timeout is applied by default so a hung invocation cannot block
    the entire test run indefinitely.
    """
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
        timeout=timeout,
    )


@pytest.mark.unit
def test_full_form_exits_zero_and_emits_result_json() -> None:
    """All required flags produce a zero exit and ModelDispatchWorkerResult JSON."""
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
            "OMN-0000",
            "omnimarket#1",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert "validated_task_description" in payload
    assert "validated_prompt_template" in payload
    assert "proposed_agent_spawn_args" in payload
    assert "collision_fence_embeds" in payload


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
def test_watcher_role_with_pr_target_exits_zero() -> None:
    """Watcher role accepts a PR-form target and exits 0."""
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
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    spawn_args = payload["proposed_agent_spawn_args"]
    assert spawn_args["team_name"] == "Omninode"
    assert spawn_args["name"] == "test-watcher"


@pytest.mark.unit
def test_collision_fences_are_embedded_in_prompt() -> None:
    """--collision-fences values surface in the rendered prompt template."""
    result = _run(
        [
            "--name",
            "fenced-fixer",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "Fix Y",
            "--targets",
            "OMN-0001",
            "omnimarket#1",
            "--collision-fences",
            "OMN-9999",
            "omnimarket#200",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    fences_blob = json.dumps(payload["collision_fence_embeds"])
    prompt_blob = payload["validated_prompt_template"]
    assert "OMN-9999" in fences_blob or "OMN-9999" in prompt_blob
    assert "omnimarket#200" in fences_blob or "omnimarket#200" in prompt_blob


@pytest.mark.unit
def test_replace_flag_is_accepted() -> None:
    """--replace is accepted at the CLI layer and exits 0 when no duplicate exists."""
    result = _run(
        [
            "--name",
            "replace-fixer",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "Replace Z",
            "--targets",
            "OMN-0002",
            "omnimarket#1",
            "--replace",
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["rejected_reason"] == ""


@pytest.mark.unit
def test_json_input_overrides_flags() -> None:
    """--json-input accepts a raw ModelDispatchWorkerCommand payload."""
    cmd_json = json.dumps(
        {
            "name": "json-fixer",
            "team": "Omninode",
            "role": "fixer",
            "scope": "Scope from JSON",
            "targets": ["OMN-0003", "omnimarket#1"],
            "collision_fences": [],
            "reports_to": "team-lead",
            "model": "sonnet",
            "replace": False,
        }
    )
    result = _run(
        [
            "--name",
            "ignored",
            "--team",
            "ignored",
            "--role",
            "fixer",
            "--scope",
            "ignored",
            "--targets",
            "IGN-0",
            "--json-input",
            cmd_json,
        ]
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert payload["proposed_agent_spawn_args"]["name"] == "json-fixer"


@pytest.mark.unit
def test_wall_clock_cap_out_of_range_exits_nonzero() -> None:
    """wall_clock_cap_min above the model-enforced max fails.

    ModelDispatchWorkerCommand caps wall_clock_cap_min at 480; 999 must be
    rejected at model-validation time.
    """
    result = _run(
        [
            "--name",
            "oob-fixer",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "OOB scope",
            "--targets",
            "OMN-0004",
            "omnimarket#1",
            "--wall-clock-cap-min",
            "999",
        ]
    )
    assert result.returncode != 0, (
        "Expected non-zero exit for out-of-bounds wall_clock_cap_min, "
        f"got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
