# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the projection contract checks over a corpus case (OMN-20567, row 13).

``run_node`` calls the canonical node runtime's ``main`` the way CI and pre-commit
reach it: with a synthetic repo tree as cwd and the case's argv. It runs in-process
(one interpreter start per case cost about a second, which put the parity suite over
the DoD verifier's per-check budget); the ``python -m`` entrypoint itself is covered
by the subprocess tests in ``test_projection_contract_check_node.py``. It returns the
same observation shape the golden file recorded from the original scripts, so a test
can compare them field for field.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import TypedDict

from omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check import (
    main,
)
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


def run_main(cwd: Path, argv: list[str]) -> tuple[int, str, str]:
    """Run the runtime's ``main`` in ``cwd``; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.chdir(cwd),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def _observe(
    rc: int, stdout: str, stderr: str, tmp: Path, case: CorpusCase
) -> Observation:
    baseline = tmp / BASELINE_REL
    after = (
        _normalize_regenerate_line(baseline.read_text(encoding="utf-8"))
        if case.rule == "cursor" and baseline.exists()
        else None
    )
    return {
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
        "baseline_after": after,
    }


def run_node(tmp: Path, case: CorpusCase) -> Observation:
    materialize(tmp, case)
    rc, stdout, stderr = run_main(tmp, ["--rule", case.rule, *case.argv])
    return _observe(rc, stdout, stderr, tmp, case)
