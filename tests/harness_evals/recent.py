# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen receipt-derived cohort; reduced cases, never live external mutations."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

MANIFEST = (
    Path(__file__).resolve().parents[2] / "benchmarks/harness_evals/recent.v1.json"
)


def recent_tasks() -> list[dict[str, Any]]:
    return json.loads(MANIFEST.read_text())["items"]


def recent_digest() -> str:
    return hashlib.sha256(
        MANIFEST.read_bytes() + inspect.getsource(check_recent).encode()
    ).hexdigest()


def host_answer() -> dict[str, Any]:
    return {
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "python_major": sys.version_info.major,
        "cpu_count": os.cpu_count(),
    }


def check_recent(task: dict[str, Any], root: Path) -> bool:
    """Compare frozen outputs and protected inputs, or execute immutable tests."""
    if __package__:
        from .fixtures import _exec
    else:
        from fixtures import _exec

    try:
        expected = task["expected_files"]
        for name, content in task["files"].items():
            if name not in expected and (root / name).read_text() != content:
                return False
        if task["task_class"] == "host-read-only":
            return {p.name for p in root.iterdir()} - {".git"} == {
                "host-request.json",
                "answer.json",
            } and json.loads((root / "answer.json").read_text()) == host_answer()
        if task["task_class"] == "failing-test-fix":
            return _exec(
                root,
                "import runpy,sys; sys.path.insert(0,'.'); runpy.run_path('test_subject.py')",
            )
        return all(
            json.loads((root / name).read_text()) == json.loads(value)
            if name.endswith(".json")
            else (root / name).read_text() == value
            for name, value in expected.items()
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False


def oracle_recent(task: dict[str, Any], root: Path) -> None:
    for name, value in task["expected_files"].items():
        (root / name).write_text(value)
    if task["task_class"] == "host-read-only":
        (root / "answer.json").write_text(json.dumps(host_answer()))


def check_task(task: dict[str, Any], root: Path, *, git_dir: str | None = None) -> bool:
    if task.get("cohort") == "recent" and task["task_class"] != "rebase-conflict":
        return check_recent(task, root)
    if __package__:
        from .fixtures import check
    else:
        from fixtures import check
    return check(task, root, git_dir=git_dir)
