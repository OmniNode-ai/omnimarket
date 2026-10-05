# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the projection contract checks over a corpus case (OMN-20567, row 13).

``run_node`` executes the canonical node runtime the way CI and pre-commit do: with
a synthetic repo tree as cwd. It returns the same observation shape the golden file
recorded from the original scripts, so a test can compare them field for field.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from tests.ci.projection_contract_check_corpus import CorpusCase

REPO_ROOT = Path(__file__).resolve().parents[2]

NODE_MODULE = (
    "omnimarket.nodes.node_contract_projection_check_effect."
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


def _normalize_regenerate_line(text: str) -> str:
    """The baseline header names the command that regenerates it; that command moved
    from the deleted script to the node runtime on purpose, so it is not compared."""
    return "\n".join(
        "#   <regenerate command>" if line.startswith("#   uv run python") else line
        for line in text.split("\n")
    )


def _observe(
    proc: subprocess.CompletedProcess[str], tmp: Path, case: CorpusCase
) -> Observation:
    baseline = tmp / BASELINE_REL
    after = (
        _normalize_regenerate_line(baseline.read_text(encoding="utf-8"))
        if case.rule == "cursor" and baseline.exists()
        else None
    )
    return {
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "baseline_after": after,
    }


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
