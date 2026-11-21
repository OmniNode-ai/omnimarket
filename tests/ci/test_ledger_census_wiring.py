# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-file census ratchet is enforced by a hook and a CI step that both name its ratchet file.

A mechanism audit judges a ratchet file wired only when a pre-commit hook and a required CI job
name that file. The hook alone left the ratchet red-able on a developer machine and green in CI.
"""

from pathlib import Path

import yaml

from scripts.ci import ci_summary_gate as gate

ROOT = Path(__file__).resolve().parents[2]
RATCHET = "src/omnimarket/nodes/node_work_ledger_file_census_effect/ratchet.yaml"
MODULE = "omnimarket.nodes.node_work_ledger_file_census_effect"
HOOK_ID = "ledger-file-census"


def names_ratchet(command: str) -> bool:
    """True when a command runs the census module and passes it the ratchet file."""
    return MODULE in command and f"--ratchet-file {RATCHET}" in command


def hook_entry() -> str:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    entries = [
        hook["entry"]
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == HOOK_ID
    ]
    assert len(entries) == 1
    return str(entries[0])


def strict_job_commands(job_id: str) -> list[str]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return [
        str(step["run"]) for step in workflow["jobs"][job_id]["steps"] if "run" in step
    ]


def test_pre_commit_hook_names_the_ratchet_file() -> None:
    assert names_ratchet(hook_entry())


def test_lint_job_runs_the_census_with_the_ratchet_file() -> None:
    assert any(names_ratchet(cmd) for cmd in strict_job_commands("lint"))


def test_lint_job_gates_ci_summary() -> None:
    assert "lint" in gate.STRICT_GATE_JOBS


def test_names_ratchet_refuses_a_command_without_the_file() -> None:
    bare = f"uv run python -m {MODULE} --repos omnimarket --directory omnimarket=."
    assert not names_ratchet(bare)
    assert names_ratchet(f"{bare} --ratchet-file {RATCHET}")
