# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reads and writes of the release cut tick: git, gh, the ledger command, the notification and the state files.

Every command goes through a ``Runner`` so a test can stand a recorded one in; the one mutation of a
tick is the ``release-cut.yml`` dispatch in ``dispatch_cut``.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_release_cut_tick_effect.handlers.release_cut_decision import (
    BOOKKEEPING,
    PACKAGED_PATHS,
    RELEASE_TAG,
    REPO,
    SLUG,
    WORKFLOW,
    cell,
)

#: What the tick copies out of the omnibase_infra ``origin/dev``: the train and every sibling it loads.
INFRA_TREE = (
    "scripts",
    "config/release_train_policy.yaml",
    "config/deploy_lane_routing.yaml",
)
VALIDATOR = ".github/actions/deploy-gate/validate_pr_deploy_required.py"


class ModelCompleted(BaseModel):
    """One finished command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], ModelCompleted]
Appender = Callable[[Sequence[str], Path, str], tuple[bool, str]]
Notifier = Callable[[str, str, str], bool]


def run_command(
    argv: Sequence[str], *, timeout: int = 300, cwd: Path | None = None
) -> ModelCompleted:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ModelCompleted(returncode=127, stderr=f"{type(exc).__name__}: {exc}")
    return ModelCompleted(
        returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
    )


def _git(clone: Path, *args: str, run: Runner) -> ModelCompleted:
    return run(["git", "-C", str(clone), *args])


def refresh_and_materialize(registry_root: Path, dest: Path, run: Runner) -> str | None:
    """Fetch the three clones the decision reads and copy the train out of infra ``origin/dev``.

    Returns a reason string when a step failed, else None. A fetch or archive that fails is a
    BLOCK, never a decision made on a stale or empty tree.
    """
    for repo, refspec in (
        ("omnimarket", "dev"),
        ("omnibase_infra", "dev"),
        ("omniclaude", "dev"),
    ):
        done = _git(
            registry_root / repo,
            "fetch",
            "--quiet",
            "--tags",
            "origin",
            refspec,
            run=run,
        )
        if done.returncode != 0:
            return f"fetch of {repo} origin/{refspec} failed: {cell(done.stderr)}"
    archive = run(
        [
            "git",
            "-C",
            str(registry_root / "omnibase_infra"),
            "archive",
            "--format=tar",
            "-o",
            str(dest / "infra.tar"),
            "origin/dev",
            *INFRA_TREE,
        ]
    )
    if archive.returncode != 0:
        return (
            f"git archive of omnibase_infra origin/dev failed: {cell(archive.stderr)}"
        )
    untar = run(["tar", "-x", "-C", str(dest), "-f", str(dest / "infra.tar")])
    if untar.returncode != 0:
        return f"unpacking the train failed: {cell(untar.stderr)}"
    show = _git(
        registry_root / "omniclaude", "show", f"origin/dev:{VALIDATOR}", run=run
    )
    if show.returncode != 0 or not show.stdout.strip():
        return f"omniclaude origin/dev has no {VALIDATOR}: {cell(show.stderr)}"
    (dest / "validator.py").write_text(show.stdout, encoding="utf-8")
    return None


def write_cut_policy(source: Path, dest: Path) -> str | None:
    """Copy the train policy with omnimarket's mode set to ``cut``. None on success, else why not.

    The train carries no default for an undeclared repo, and neither does this: a policy with no
    omnimarket entry is refused, never guessed.
    """
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return f"the train policy is unreadable: {cell(str(exc))}"
    repos = data.get("repos") if isinstance(data, dict) else None
    entry = repos.get(REPO) if isinstance(repos, dict) else None
    if not isinstance(entry, dict):
        return f"the train policy declares no {REPO} entry"
    entry["mode"] = "cut"
    dest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return None


def oldest_unreleased_age_hours(
    registry_root: Path, latest_tag: str, now: datetime, run: Runner
) -> float | None:
    """Age in hours of the oldest unreleased packaged commit, bookkeeping excluded."""
    if not RELEASE_TAG.match(latest_tag):
        return None
    done = _git(
        registry_root / REPO,
        "log",
        "--no-merges",
        "--format=%cI%x09%s",
        f"{latest_tag}..origin/dev",
        "--",
        *PACKAGED_PATHS,
        run=run,
    )
    if done.returncode != 0:
        return None
    oldest: datetime | None = None
    for line in done.stdout.splitlines():
        stamp_text, _, subject = line.partition("\t")
        if any(pattern.match(subject) for pattern in BOOKKEEPING):
            continue
        try:
            when = datetime.fromisoformat(stamp_text)
        except ValueError:
            continue
        if oldest is None or when < oldest:
            oldest = when
    if oldest is None:
        return None
    return (now - oldest).total_seconds() / 3600.0


def _runs(done: ModelCompleted) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(done.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return [item for item in parsed if isinstance(item, dict)]


def active_release_runs(run: Runner) -> tuple[list[dict[str, Any]] | None, str]:
    """Queued or in-progress release-cut runs. None plus a reason when the read failed."""
    rows: list[dict[str, Any]] = []
    for status in ("queued", "in_progress"):
        done = run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                SLUG,
                "--workflow",
                WORKFLOW,
                "--status",
                status,
                "--json",
                "databaseId,url,headSha,status",
            ]
        )
        if done.returncode != 0:
            return None, f"gh run list ({status}) failed: {cell(done.stderr)}"
        parsed = _runs(done)
        if parsed is None:
            return None, f"gh run list ({status}) returned unreadable JSON"
        rows.extend(parsed)
    return rows, ""


def previous_run_verdict(state: dict[str, Any], run: Runner) -> tuple[str, str]:
    """(``ok``, ``failed``, ``pending`` or ``none``, detail) for the last dispatched run."""
    run_id = state.get("dispatched_run_id")
    if not run_id or state.get("dispatched_run_checked"):
        return "none", ""
    done = run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            SLUG,
            "--json",
            "status,conclusion,url",
        ]
    )
    if done.returncode != 0:
        return (
            "failed",
            f"the dispatched run {run_id} could not be read: {cell(done.stderr)}",
        )
    try:
        row = json.loads(done.stdout)
    except json.JSONDecodeError:
        return "failed", f"the dispatched run {run_id} returned unreadable JSON"
    if row.get("status") != "completed":
        return "pending", str(row.get("url", ""))
    if row.get("conclusion") == "success":
        return "ok", str(row.get("url", ""))
    return "failed", f"run {row.get('url')} concluded {row.get('conclusion')}"


def _recent_runs(run: Runner) -> list[dict[str, Any]] | None:
    done = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            SLUG,
            "--workflow",
            WORKFLOW,
            "--limit",
            "5",
            "--json",
            "databaseId,url",
        ]
    )
    if done.returncode != 0:
        return None
    parsed = _runs(done)
    if parsed is None:
        return None
    return [item for item in parsed if "databaseId" in item]


def dispatch_cut(run: Runner) -> tuple[str, str]:
    """Dispatch release-cut.yml and find the run it started. ``("", reason)`` on failure."""
    before = {row["databaseId"] for row in _recent_runs(run) or []}
    done = run(["gh", "workflow", "run", WORKFLOW, "--repo", SLUG, "--ref", "dev"])
    if done.returncode != 0:
        return "", f"gh workflow run {WORKFLOW} failed: {cell(done.stderr)}"
    for _ in range(6):
        for row in _recent_runs(run) or []:
            if row["databaseId"] not in before:
                return f"{row['databaseId']} {row['url']}", ""
        run(["sleep", "5"])
    return "", "the dispatch was accepted but no new run appeared within 30 seconds"


def append_row(command: Sequence[str], ledger: Path, row: str) -> tuple[bool, str]:
    """Append one row by running ``<command> <ledger> --append <row>``."""
    if not command:
        return False, (
            "no ledger command: set ONEX_LEDGER_COMMAND to the onex-ledger command "
            "line, or pass --ledger-lock (no default)"
        )
    done = run_command([*command, str(ledger), "--append", row], timeout=120)
    detail = (done.stderr.strip() or done.stdout.strip())[:400]
    return done.returncode == 0, f"rc={done.returncode} {detail}".strip()


def notify(osascript: str, title: str, message: str) -> bool:
    done = run_command(
        [
            osascript,
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e",
            "end run",
            message,
            title,
        ],
        timeout=30,
    )
    return done.returncode == 0


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
