# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared setup of the central test harness tests: a real git repository, a real lab host
on the in-memory bus, and an overlay whose cases run real commands at a real commit."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
)

HOST = "h202"
REPO = "OmniNode-ai/demo"
T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.1, mem_available_bytes=8 * 1024**3, tools=("git",)
        )


def make_origin(tmp_path: Path) -> tuple[str, str]:
    """A repository with a passing probe and a failing probe; returns (url_base, sha)."""
    origin = tmp_path / "origin" / "OmniNode-ai" / "demo"
    origin.mkdir(parents=True)
    _git("init", "-q", cwd=origin)
    (origin / "passes.py").write_text("print('ok')\n")
    (origin / "fails.py").write_text("import sys\nprint('broken input')\nsys.exit(3)\n")
    _git("add", ".", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin)
    (origin.parent / "demo.git").symlink_to(origin / ".git")
    return f"file://{tmp_path / 'origin'}", sha


def overlay_document(
    sha: str,
    result_path: Path,
    *,
    golden: str = "passes.py",
    error: str = "fails.py",
    host: str = HOST,
    receipt_slack_seconds: int = 5,
    max_age_seconds: int = 900,
) -> dict[str, object]:
    python = sys.executable
    return {
        "overlay_revision": "test.1",
        "piece": "central-test-harness",
        "tag": "drill",
        "lane": "harness-drill",
        "schedule": {"workflow": "lab-test-harness", "interval_seconds": 300},
        "max_age_seconds": max_age_seconds,
        "receipt_slack_seconds": receipt_slack_seconds,
        "current_result_path": str(result_path),
        "cases": [
            {
                "case_id": "golden-passes",
                "kind": "golden",
                "repo": REPO,
                "commit_sha": sha,
                "argv": [python, golden],
                "target_host": host,
                "timeout_seconds": 60,
            },
            {
                "case_id": "error-broken-input",
                "kind": "error",
                "repo": REPO,
                "commit_sha": sha,
                "argv": [python, error],
                "target_host": host,
                "timeout_seconds": 60,
            },
        ],
    }


def make_overlay(
    sha: str,
    result_path: Path,
    *,
    golden: str = "passes.py",
    error: str = "fails.py",
    receipt_slack_seconds: int = 5,
    max_age_seconds: int = 900,
) -> ModelLabTestHarnessOverlay:
    return ModelLabTestHarnessOverlay.model_validate(
        overlay_document(
            sha,
            result_path,
            golden=golden,
            error=error,
            receipt_slack_seconds=receipt_slack_seconds,
            max_age_seconds=max_age_seconds,
        )
    )
