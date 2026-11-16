# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket's deploy gate takes the PR body from the event payload.

The gate step's ``run`` script no longer shells out to ``gh pr view`` for the
pull request body.  The body is supplied through the step's ``env`` block as
``PR_BODY``, which GitHub fills from ``github.event.pull_request.body``.  This
keeps the step free of any GitHub API read for the body, so it works on a
runner that cannot reach the GitHub API.

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


def _run_gate_step(
    tmp_path: Path, title: str, body: str, *, marker: Path | None = None
) -> list[str]:
    """Run the gate's script with stub gh and python; return the validator argv."""
    gate = _gate_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "validator-argv.txt"
    _write_exe(
        bin_dir / "gh",
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "pr diff" ]; then\n'
        "  echo src/omnimarket/nodes/node_e2e_bridge_effect/contract.yaml\n"
        'elif [ "$1 $2" = "pr view" ]; then\n'
        + (f"  touch {shlex.quote(str(marker))}\n" if marker is not None else "")
        + "  exit 1\n"
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
        "PR_BODY": body,
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


def test_body_env_comes_from_the_event_payload() -> None:
    gate = _gate_step()
    assert gate["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
    assert "github.event.pull_request.body" not in gate["run"]


def test_gate_script_makes_no_pr_view_call() -> None:
    gate = _gate_step()
    assert "gh pr view" not in gate["run"]


def test_validator_gets_the_body_when_gh_pr_view_fails(tmp_path: Path) -> None:
    marker = tmp_path / "pr-view-called"
    argv = _run_gate_step(tmp_path, TITLE, BODY, marker=marker)
    pr_body = _pr_body_arg(argv)
    assert pr_body.endswith(BODY)
    assert not marker.exists()


def test_empty_body_still_reaches_the_validator(tmp_path: Path) -> None:
    argv = _run_gate_step(tmp_path, TITLE, "")
    pr_body = _pr_body_arg(argv)
    assert pr_body.startswith(TITLE)


def _payload_body(event: dict[str, Any]) -> str:
    """What Actions puts in ``github.event.pull_request.body``: null becomes ''."""
    body = event["pull_request"]["body"]
    return "" if body is None else str(body)


@pytest.mark.parametrize(
    ("event_body", "expected"),
    [(BODY, BODY), (None, "")],
    ids=["body-present", "body-null"],
)
def test_event_payload_body_reaches_the_validator(
    tmp_path: Path, event_body: str | None, expected: str
) -> None:
    """The seam: the payload's own body field is what the validator is handed."""
    event = {"pull_request": {"number": 3817, "title": TITLE, "body": event_body}}
    argv = _run_gate_step(tmp_path, TITLE, _payload_body(event))
    # Command substitution in the script strips trailing newlines.
    assert _pr_body_arg(argv) == f"{TITLE}\n\n{expected}".rstrip("\n")
