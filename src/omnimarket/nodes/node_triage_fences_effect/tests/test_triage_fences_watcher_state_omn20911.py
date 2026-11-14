# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fence list reads the PR watcher's state and makes no GitHub call (OMN-20911).

Measured 2026-10-13: the 14:45Z dod-closeout run derived the fence list at 14:45:20 and the
host's gh call log shows 14 ``gh pr list`` calls at 14:45:23 to 14:45:30, one per registry
repository, from the default ``--open-prs`` path. These tests run the node as production runs it
(its console entry, in a subprocess, with a ``gh`` first on PATH that records every call) and
require zero recorded calls.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_NODE_CLI = "omnimarket.nodes.node_triage_fences_effect.runtime_triage_fences_cli"
# Repositories the overlay of these tests sweeps; the production list is the overlays repository's.
DEFAULT_REPOS = (
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omniclaude",
    "omnimarket",
    "omnidash",
    "omniintelligence",
    "omnimemory",
)
TICK = "2026-10-13T14:45:00Z"
RESYNC = "2026-10-13T14:10:00Z"
NOW = "2026-10-13T14:48:00Z"
LEDGER_LINE = "2026-10-13T14:00:00Z | CLAIM | lane=alpha | ticket=OMN-1001 | actor=claude | scope=x"


def _fact(repo: str, number: int, **over: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "state": "OPEN",
        "title": f"fix(OMN-{number}): a change",
        "head_ref": f"jonah/omn-{number}-change",
        "updated_at": "2026-10-13T14:30:00Z",
        "body": "",
    }
    facts.update(over)
    return {"facts": facts}


def _state(prs: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema": 1,
        "last_tick": TICK,
        "last_full_resync": RESYNC,
        "operator": "operator",
        "repos": {repo: {"default_branch": "main"} for repo in DEFAULT_REPOS},
        "prs": {f"{p['facts']['repo']}#{p['facts']['number']}": p for p in prs},
    }
    state.update(over)
    return state


@pytest.fixture
def gh_log(tmp_path: Path) -> Path:
    """A ``gh`` first on PATH that records its argv and fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-calls.txt"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n', encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return log


def _overlay_root(tmp_path: Path) -> Path:
    """The node overlay on disk, as production supplies it through ONEX_SKILL_OVERLAY_ROOTS."""
    root = tmp_path / "overlay-root"
    (root / "node_triage_fences_effect").mkdir(parents=True, exist_ok=True)
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    overlay = {
        "stale_after_minutes": 2880,
        "pr_window_hours": 6,
        "repos": list(DEFAULT_REPOS),
        "receipt_dir": str(receipts),
        "running_receipt_max_age_minutes": 240,
        "watcher_state": None,
    }
    (root / "node_triage_fences_effect" / "overlay.yaml").write_text(
        json.dumps(overlay), encoding="utf-8"
    )
    return root


def _run(
    tmp_path: Path, gh_log: Path, state: dict[str, Any] | None, *extra: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text(LEDGER_LINE + "\n", encoding="utf-8")
    state_file = tmp_path / "state.json"
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{gh_log.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "ONEX_PR_WATCHER_STATE": str(state_file),
        "ONEX_SKILL_OVERLAY_ROOTS": str(_overlay_root(tmp_path)),
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            _NODE_CLI,
            str(ledger),
            "--open-prs",
            "--now",
            NOW,
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    return done, json.loads(done.stdout)


def _gh_calls(gh_log: Path) -> list[str]:
    return gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []


def test_the_recording_gh_records_a_call(gh_log: Path) -> None:
    """Positive control: a zero in the tests below means no call, not a broken recorder."""
    env = {
        **os.environ,
        "PATH": f"{gh_log.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
    }
    subprocess.run(["gh", "pr", "list"], env=env, check=False, capture_output=True)
    assert _gh_calls(gh_log) == ["pr list"]


def test_a_fresh_watcher_state_answers_every_default_repo_with_zero_gh_calls(
    tmp_path: Path, gh_log: Path
) -> None:
    prs = [_fact("omnibase_infra", 3156), _fact("omnimemory", 40)]
    _, report = _run(tmp_path, gh_log, _state(prs))
    assert _gh_calls(gh_log) == []
    assert report["errors"] == []
    assert {f["repo"] for f in report["open_pr_fences"]} == {
        "omnibase_infra",
        "omnimemory",
    }
    assert set(report["fenced_tickets"]) == {"OMN-1001", "OMN-3156", "OMN-40"}


def test_the_rows_come_from_the_state_open_prs_only_with_a_built_url(
    tmp_path: Path, gh_log: Path
) -> None:
    prs = [
        _fact("omnibase_infra", 12),
        _fact("omnibase_infra", 7, state="MERGED"),
        _fact("omnibase_infra", 9, state="CLOSED"),
        _fact("omnimarket", 3, title="chore: no ticket here", head_ref="x/none"),
    ]
    _, report = _run(
        tmp_path, gh_log, _state(prs), "--repos", "omnibase_infra,omnimarket"
    )
    fences = report["open_pr_fences"]
    assert [(f["repo"], f["number"]) for f in fences] == [("omnibase_infra", 12)]
    assert fences[0]["url"] == "https://github.com/OmniNode-ai/omnibase_infra/pull/12"
    assert fences[0]["matched_on"] == "title"
    assert _gh_calls(gh_log) == []


def test_a_stale_state_is_reported_per_repo_and_is_not_read_from_github(
    tmp_path: Path, gh_log: Path
) -> None:
    stale = _state([_fact("omnibase_infra", 1)], last_tick="2026-10-13T14:30:00Z")
    _, report = _run(tmp_path, gh_log, stale, "--repos", "omnibase_infra,omnimarket")
    assert _gh_calls(gh_log) == []
    assert report["open_pr_fences"] == []
    assert "OMN-1001" in report["fenced_tickets"], (
        "the ledger fences survive an unreadable state"
    )
    assert len(report["errors"]) == 2
    assert all("stale" in e and "last_tick" in e for e in report["errors"])


def test_a_stale_full_resync_is_refused_too(tmp_path: Path, gh_log: Path) -> None:
    stale = _state(
        [_fact("omnibase_infra", 1)], last_full_resync="2026-10-13T11:00:00Z"
    )
    _, report = _run(tmp_path, gh_log, stale, "--repos", "omnibase_infra")
    assert _gh_calls(gh_log) == []
    assert report["errors"]
    assert "last_full_resync" in report["errors"][0]


def test_a_missing_state_file_is_an_error_not_an_empty_list_or_a_gh_read(
    tmp_path: Path, gh_log: Path
) -> None:
    _, report = _run(tmp_path, gh_log, None, "--repos", "omnibase_infra")
    assert _gh_calls(gh_log) == []
    assert report["open_pr_fences"] == []
    assert report["errors"]
    assert "not readable" in report["errors"][0]


def test_a_repository_the_state_does_not_list_is_an_error_for_that_repository(
    tmp_path: Path, gh_log: Path
) -> None:
    state = _state([_fact("omnibase_infra", 1)])
    del state["repos"]["omnimarket"]
    _, report = _run(tmp_path, gh_log, state, "--repos", "omnibase_infra,omnimarket")
    assert _gh_calls(gh_log) == []
    assert [f["repo"] for f in report["open_pr_fences"]] == ["omnibase_infra"]
    assert len(report["errors"]) == 1
    assert "omnimarket" in report["errors"][0]
    assert "not a repository" in report["errors"][0]


def test_another_schema_is_refused(tmp_path: Path, gh_log: Path) -> None:
    _, report = _run(tmp_path, gh_log, _state([_fact("omnibase_infra", 1)], schema=2))
    assert _gh_calls(gh_log) == []
    assert report["errors"]
    assert "schema" in report["errors"][0]


def test_the_ledger_only_run_reads_neither_the_state_nor_github(
    tmp_path: Path, gh_log: Path
) -> None:
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text(LEDGER_LINE + "\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{gh_log.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "ONEX_PR_WATCHER_STATE": str(tmp_path / "does-not-exist.json"),
        "ONEX_SKILL_OVERLAY_ROOTS": str(_overlay_root(tmp_path)),
    }
    done = subprocess.run(
        [sys.executable, "-m", _NODE_CLI, str(ledger), "--now", NOW],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["errors"] == []
    assert _gh_calls(gh_log) == []
