# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the projection contract checks over a corpus case (OMN-20567, row 13).

``run_script`` executes one of the original scripts exactly as CI and pre-commit
did: copied into a synthetic repo tree and run with that tree as cwd. ``run_node``
executes the canonical node runtime the same way. Both return the same
observation shape so a test can compare them field for field.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from tests.ci.projection_contract_check_corpus import CorpusCase

REPO_ROOT = Path(__file__).resolve().parents[2]

SCRIPT_FOR_RULE: dict[str, str] = {
    "access": "scripts/ci/check_projection_contract_access.py",
    "dlq": "scripts/ci/check_projection_dlq_path.py",
    "cursor": "scripts/validation/check_projection_cursor_declared.py",
}

NODE_MODULE = (
    "omnimarket.nodes.node_projection_contract_check_compute."
    "runtime_projection_contract_check"
)

BASELINE_REL = "scripts/validation/projection_cursor_baseline.txt"


class Observation(TypedDict):
    rc: int
    stdout: str
    stderr: str
    baseline_after: str | None


def materialize(tmp: Path, case: CorpusCase) -> None:
    for rel, text in case.files.items():
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _observe(
    proc: subprocess.CompletedProcess[str], tmp: Path, case: CorpusCase
) -> Observation:
    baseline = tmp / BASELINE_REL
    after = (
        baseline.read_text(encoding="utf-8")
        if case.rule == "cursor" and baseline.exists()
        else None
    )
    return {
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "baseline_after": after,
    }


def run_script(tmp: Path, case: CorpusCase) -> Observation:
    materialize(tmp, case)
    rel = SCRIPT_FOR_RULE[case.rule]
    target = tmp / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / rel, target)
    proc = subprocess.run(
        [sys.executable, str(target), *case.argv],
        cwd=tmp,
        capture_output=True,
        text=True,
        check=False,
    )
    return _observe(proc, tmp, case)


def run_node(tmp: Path, case: CorpusCase) -> Observation:
    materialize(tmp, case)
    proc = subprocess.run(
        [sys.executable, "-m", NODE_MODULE, "--rule", case.rule, *case.argv],
        cwd=tmp,
        capture_output=True,
        text=True,
        check=False,
    )
    return _observe(proc, tmp, case)
