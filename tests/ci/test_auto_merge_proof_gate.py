# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""auto-merge.yml arms a pull request only after the proof gate answers PROVEN at the exact head.

A pull request armed early merges itself at green and bypasses every gate: omnimarket#6765 was armed by this workflow
on open and merged with a Lab comment naming another commit. The arming path now reads the pull request's text and
changed files and asks the proof gate node; any answer but PROVEN leaves the pull request unarmed. The structural
tests pin where the gate sits; the behavioural tests run the workflow's own step script with a stub ``gh``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = ROOT / ".github" / "workflows" / "auto-merge.yml"
HEAD = "a" * 40
OTHER = "b" * 40
PROVEN = "steps.proof_gate.outputs.proven == 'true'"


def _steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(_WORKFLOW.read_text())
    steps: list[dict[str, Any]] = data["jobs"]["auto-merge"]["steps"]
    return steps


def _named(name: str) -> dict[str, Any]:
    found = [s for s in _steps() if s.get("name") == name]
    assert len(found) == 1, name
    return found[0]


def _index(step_id: str) -> int:
    return next(i for i, s in enumerate(_steps()) if s.get("id") == step_id)


def test_a_proof_gate_step_exists_and_runs_before_arming() -> None:
    gate = _index("proof_gate")
    assert gate > _index("hold_gate")
    arm = next(
        i for i, s in enumerate(_steps()) if s.get("name") == "Enable auto-merge"
    )
    assert gate < arm


def test_arming_and_enqueueing_need_the_gate_to_answer_proven() -> None:
    assert PROVEN in str(_named("Enable auto-merge")["if"])
    enqueue = next(s for s in _steps() if "Enqueue armed PR" in str(s.get("name")))
    assert PROVEN in str(enqueue["if"])


def test_the_gate_step_asks_the_node_at_the_live_head() -> None:
    run = str(next(s for s in _steps() if s.get("id") == "proof_gate")["run"])
    assert "omnimarket.nodes.node_landing_proof_gate_compute" in run
    assert "headRefOid" in run
    assert "--pr-text-file" in run


def test_the_existing_guards_are_unchanged() -> None:
    arm = str(_named("Enable auto-merge")["if"])
    assert "steps.hold_gate.outputs.hold == 'false'" in arm
    assert "steps.resolve.outputs.skip != 'true'" in arm
    hold = next(s for s in _steps() if s.get("id") == "hold_gate")
    assert "steps.resolve.outputs.actor ==" in str(hold["if"])
    gate = next(s for s in _steps() if s.get("id") == "proof_gate")
    assert "steps.hold_gate.outputs.hold == 'false'" in str(gate["if"])
    assert "steps.resolve.outputs.skip != 'true'" in str(gate["if"])
    base = _named("Refuse feature-base auto-merge")
    assert "steps.hold_gate.outputs.hold == 'false'" in str(base["if"])
    enqueue = next(s for s in _steps() if "Enqueue armed PR" in str(s.get("name")))
    assert "steps.hold_gate.outputs.hold != 'true'" in str(enqueue["if"])
    assert "steps.resolve.outputs.actor ==" in str(enqueue["if"])


# behaviour: the step's own script, with a stub gh ----------------------------------------------------------------
STUB = """#!/usr/bin/env bash
# a stand-in for gh: answers from files named by STUB_DIR
case "$*" in
  *headRefOid*) cat "$STUB_DIR/view.json" ;;
  *"pr view"*"--json body,comments"*) cat "$STUB_DIR/text.json" ;;
  *"/files"*) cat "$STUB_DIR/files.txt" ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
"""


def _run_gate(
    tmp_path: Path, *, comments: list[str], files: list[str], head: str = HEAD
) -> dict[str, str]:
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    gh = stub_dir / "gh"
    gh.write_text(STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    (stub_dir / "view.json").write_text(
        json.dumps({"headRefOid": head, "baseRefName": "dev"})
    )
    (stub_dir / "text.json").write_text(
        json.dumps(
            {"body": "", "comments": comments}
        )  # the shape gh prints after the step's --jq
    )
    (stub_dir / "files.txt").write_text("\n".join(files) + "\n")
    script = tmp_path / "step.sh"
    script.write_text(
        str(next(s for s in _steps() if s.get("id") == "proof_gate")["run"])
    )
    out = tmp_path / "github_output"
    out.write_text("")
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "GITHUB_OUTPUT": str(out),
        "PR": "6765",
        "REPO": "OmniNode-ai/omnimarket",
        "GH_TOKEN": "unused",
        "RUNNER_TEMP": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
    }
    done = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    return dict(ln.split("=", 1) for ln in out.read_text().splitlines() if "=" in ln)


def test_a_lab_line_naming_another_commit_does_not_arm(tmp_path: Path) -> None:
    got = _run_gate(
        tmp_path,
        comments=[f"Lab: head={OTHER}"],
        files=["src/omnimarket/nodes/node_x/handlers/handler_x.py"],
    )
    assert got["proven"] == "false"


def test_no_lab_line_does_not_arm(tmp_path: Path) -> None:
    got = _run_gate(tmp_path, comments=["looks fine"], files=["src/omnimarket/x.py"])
    assert got["proven"] == "false"


def test_control_a_lab_line_naming_the_head_arms(tmp_path: Path) -> None:
    got = _run_gate(
        tmp_path,
        comments=[f"Lab: head={HEAD}"],
        files=["src/omnimarket/nodes/node_x/handlers/handler_x.py"],
    )
    assert got["proven"] == "true"


def test_control_a_documentation_only_change_arms_without_a_lab_line(
    tmp_path: Path,
) -> None:
    got = _run_gate(tmp_path, comments=[], files=["docs/guide.md"])
    assert got["proven"] == "true"
