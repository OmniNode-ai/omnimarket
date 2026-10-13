# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket's deploy gate reads the cited ticket from the PR title (OMN-20413).

repo-evidence / dod-verify takes the cited ticket from the pull request title.
The pinned omniclaude deploy-gate action read only the body, so a runtime PR
whose title names its ticket, and whose head carries that ticket's
contracts/OMN-<n>.yaml, was refused with "cites no OMN-XXXX ticket". The
workflow now runs the same pinned validator with the title and the body.

The run step is executed here with a recording ``gh`` and ``python`` on PATH,
so the test needs no network, no omniclaude checkout and no GitHub token.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-gate.yml"
pytestmark = pytest.mark.unit

TITLE = "fix(OMN-17038): rename bridge topics and add evidence contract"
BODY = "The four bridge topics are renamed.\nNo ticket is repeated here."


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy-gate"]["steps"]
    assert isinstance(steps, list)
    return steps


def _gate_step() -> dict[str, Any]:
    gates = [s for s in _steps() if s.get("id") == "deploy_gate"]
    assert len(gates) == 1, gates
    return gates[0]


def _write_exe(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(0o755)


def _run_gate_step(tmp_path: Path, title: str, body: str) -> list[str]:
    """Run the gate's script with stub gh and python; return the validator argv."""
    gate = _gate_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "validator-argv.txt"
    body_file = tmp_path / "body.txt"
    body_file.write_text(body)
    _write_exe(
        bin_dir / "gh",
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "pr diff" ]; then\n'
        "  echo src/omnimarket/nodes/node_e2e_bridge_effect/contract.yaml\n"
        'elif [ "$1 $2" = "pr view" ]; then\n'
        f"  cat {shlex.quote(str(body_file))}\n"
        "else\n"
        "  exit 2\n"
        "fi\n",
    )
    _write_exe(
        bin_dir / "python",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-m" ]; then exit 0; fi\n'
        f'printf "%s\\0" "$@" > {shlex.quote(str(record))}\n',
    )
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "unused",
        "PR_NUMBER": "3817",
        "PR_TITLE": title,
        "GITHUB_REPOSITORY": "OmniNode-ai/omnimarket",
        "CONTRACTS_DIR": gate["env"]["CONTRACTS_DIR"],
        "VALIDATOR": gate["env"]["VALIDATOR"],
    }
    subprocess.run(
        ["bash", "-e", "-c", gate["run"]],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return record.read_text().split("\0")[:-1]


def _pr_body_arg(argv: list[str]) -> str:
    return argv[argv.index("--pr-body") + 1]


def test_title_comes_from_the_event_through_env() -> None:
    gate = _gate_step()
    assert gate["env"]["PR_TITLE"] == "${{ github.event.pull_request.title }}"
    assert "github.event.pull_request.title" not in gate["run"]


def test_validator_is_handed_the_title_and_the_body(tmp_path: Path) -> None:
    argv = _run_gate_step(tmp_path, TITLE, BODY)
    assert argv[0] == _gate_step()["env"]["VALIDATOR"]
    pr_body = _pr_body_arg(argv)
    assert pr_body.splitlines()[0] == TITLE
    assert pr_body.endswith(BODY)
    assert "OMN-17038" in pr_body
    contracts = argv[argv.index("--contracts-dir") + 1]
    assert contracts == ".pr-head/contracts"
    assert argv[argv.index("--repository") + 1] == "OmniNode-ai/omnimarket"


def test_body_evidence_lines_stay_line_anchored(tmp_path: Path) -> None:
    """The title is its own first line, so body Evidence-* lines still start lines."""
    body = "Evidence-Source: OCC#1\nEvidence-Ticket: OMN-1"
    pr_body = _pr_body_arg(_run_gate_step(tmp_path, TITLE, body))
    assert "\nEvidence-Source: OCC#1\n" in pr_body
    assert pr_body.endswith("\nEvidence-Ticket: OMN-1")


def test_validator_checkout_precedes_the_gate_on_every_pr_event() -> None:
    steps = _steps()
    gate = _gate_step()
    checkout = next(
        s
        for s in steps
        if (s.get("with") or {}).get("repository") == "OmniNode-ai/omniclaude"
    )
    assert steps.index(checkout) < steps.index(gate)
    assert checkout.get("if") == gate.get("if")
    assert checkout["with"].get("persist-credentials") is False
