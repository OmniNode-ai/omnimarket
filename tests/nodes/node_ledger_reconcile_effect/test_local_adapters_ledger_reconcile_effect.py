# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters reach the host's files, `gh`, `git` and the ledger writer the way the old tool did (OMN-20677)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.ledger_reconcile import ModelShaRef
from omnimarket.nodes.node_ledger_reconcile_effect.protocols import ReconcilePortError
from omnimarket.nodes.node_ledger_reconcile_effect.protocols import (
    local_ledger_reconcile_adapters as adapters,
)
from omnimarket.nodes.node_ledger_reconcile_effect.protocols.local_ledger_reconcile_adapters import (
    GhPullRequests,
    GitCommits,
    LedgerWriterAppender,
    LocalReconcileHost,
    SystemClock,
)

OVERLAY_TEXT = """\
github_org: Example-Org
repo_aliases:
  market: omnimarket
branch_prefixes: [lane]
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OMNI_HOME",
        "ONEX_LEDGER_PATH",
        adapters.OVERLAY_ENV,
        adapters.OVERLAY_ROOTS_ENV,
        adapters.APPEND_COMMAND_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


# --- prerequisites -----------------------------------------------------------


@pytest.mark.parametrize("missing", ["OMNI_HOME", "ONEX_LEDGER_PATH"])
def test_a_missing_required_variable_is_a_prerequisite_failure(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(adapters.shutil, "which", lambda tool: "/bin/" + tool)
    for name in ("OMNI_HOME", "ONEX_LEDGER_PATH"):
        if name != missing:
            monkeypatch.setenv(name, "/x")
    assert (
        LocalReconcileHost().prerequisites()
        == f"{missing} is not set (required, no default)"
    )


@pytest.mark.parametrize(
    ("absent", "message"),
    [
        ("gh", "gh CLI not on PATH — live PR verification is impossible"),
        ("git", "git not on PATH"),
    ],
)
def test_a_missing_tool_is_a_prerequisite_failure(
    monkeypatch: pytest.MonkeyPatch, absent: str, message: str
) -> None:
    monkeypatch.setenv("OMNI_HOME", "/x")
    monkeypatch.setenv("ONEX_LEDGER_PATH", "/y")
    monkeypatch.setattr(
        adapters.shutil,
        "which",
        lambda tool: None if tool == absent else "/bin/" + tool,
    )
    assert LocalReconcileHost().prerequisites() == message


def test_a_host_with_everything_has_no_prerequisite_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_HOME", "/x")
    monkeypatch.setenv("ONEX_LEDGER_PATH", "/y")
    monkeypatch.setattr(adapters.shutil, "which", lambda tool: "/bin/" + tool)
    assert LocalReconcileHost().prerequisites() == ""


# --- ledger, archives, clones, roster -----------------------------------------


def test_the_ledger_and_the_archives_beside_it_are_read_in_name_order(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("live\n")
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ROLLING_WORK_LEDGER_2026-09-02-split.md").write_text("second\n")
    (archive / "ROLLING_WORK_LEDGER_2026-09-01-split.md").write_text("first\n")
    (archive / "NOTES.md").write_text("not an archive\n")
    live, archives = LocalReconcileHost().read_ledger(ledger)
    assert (live.name, live.text) == ("ROLLING_WORK_LEDGER.md", "live\n")
    assert [(a.name, a.text) for a in archives] == [
        ("ROLLING_WORK_LEDGER_2026-09-01-split.md", "first\n"),
        ("ROLLING_WORK_LEDGER_2026-09-02-split.md", "second\n"),
    ]


def test_a_ledger_with_no_archive_directory_reads_alone(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("live\n")
    assert LocalReconcileHost().read_ledger(ledger)[1] == ()


def test_a_missing_ledger_raises_a_port_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcilePortError, match="live ledger missing"):
        LocalReconcileHost().read_ledger(tmp_path / "absent.md")


def test_clones_are_the_directories_that_hold_a_git_entry(tmp_path: Path) -> None:
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    (tmp_path / "worktree_clone").mkdir()
    (tmp_path / "worktree_clone" / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "plain").mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert sorted(LocalReconcileHost().clone_names(tmp_path)) == [
        "alpha",
        "worktree_clone",
    ]


def test_a_roster_is_split_on_space_and_comma_with_comments_and_markup_removed(
    tmp_path: Path,
) -> None:
    roster = tmp_path / "live.txt"
    roster.write_text(
        "# the live lanes\nalpha, beta  # trailing note\n`gamma`\n**delta**\n\n"
    )
    assert LocalReconcileHost().read_roster(roster) == frozenset(
        {"alpha", "beta", "gamma", "delta"}
    )


def test_a_missing_roster_file_raises_a_port_error(tmp_path: Path) -> None:
    with pytest.raises(ReconcilePortError, match="--live-lanes file missing"):
        LocalReconcileHost().read_roster(tmp_path / "absent.txt")


# --- overlay ------------------------------------------------------------------


def test_the_overlay_pointer_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "pointed.yaml"
    overlay.write_text(OVERLAY_TEXT)
    monkeypatch.setenv(adapters.OVERLAY_ENV, str(overlay))
    loaded = LocalReconcileHost().overlay()
    assert loaded.github_org == "Example-Org"
    assert loaded.repo_aliases == {"market": "omnimarket"}
    assert loaded.branch_prefixes == ("lane",)


def test_the_overlay_is_found_under_the_first_root_that_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty, full = tmp_path / "empty", tmp_path / "full"
    (full / adapters.OVERLAY_NODE).mkdir(parents=True)
    (full / adapters.OVERLAY_NODE / "overlay.yaml").write_text(OVERLAY_TEXT)
    empty.mkdir()
    monkeypatch.setenv(
        adapters.OVERLAY_ROOTS_ENV, os.pathsep.join(["", str(empty), str(full)])
    )
    assert LocalReconcileHost().overlay().github_org == "Example-Org"


def test_no_overlay_is_a_hard_stop_not_an_empty_default() -> None:
    with pytest.raises(ReconcilePortError, match="no ledger-reconcile overlay"):
        LocalReconcileHost().overlay()


def test_an_empty_pointer_is_a_hard_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(adapters.OVERLAY_ENV, "")
    with pytest.raises(ReconcilePortError, match="empty overlay pointer"):
        LocalReconcileHost().overlay()


def test_a_pointer_never_falls_through_to_the_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / adapters.OVERLAY_NODE).mkdir()
    (tmp_path / adapters.OVERLAY_NODE / "overlay.yaml").write_text(OVERLAY_TEXT)
    monkeypatch.setenv(adapters.OVERLAY_ROOTS_ENV, str(tmp_path))
    monkeypatch.setenv(adapters.OVERLAY_ENV, str(tmp_path / "absent.yaml"))
    with pytest.raises(ReconcilePortError, match="is unusable"):
        LocalReconcileHost().overlay()


@pytest.mark.parametrize(
    "text", ["- not\n- a mapping\n", "github_org: [unclosed\n", "repo_aliases: {}\n"]
)
def test_an_unusable_overlay_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    overlay = tmp_path / "bad.yaml"
    overlay.write_text(text)
    monkeypatch.setenv(adapters.OVERLAY_ENV, str(overlay))
    with pytest.raises(ReconcilePortError, match="is unusable"):
        LocalReconcileHost().overlay()


def test_the_clock_reads_whole_seconds_in_utc() -> None:
    now = SystemClock().now()
    assert now.microsecond == 0
    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


# --- gh -------------------------------------------------------------------------


def _completed(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


def test_a_merged_pr_is_read_from_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        del timeout
        seen.append(cmd)
        return _completed(
            0,
            json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-09-22T11:00:00Z",
                    "mergeCommit": {"oid": "a" * 40},
                    "title": "fix(OMN-1): work",
                }
            ),
        )

    monkeypatch.setattr(adapters, "_run", fake)
    fact = GhPullRequests().pr("Example-Org", "omnimarket", 11)
    assert (fact.state, fact.merged_at, fact.merge_sha, fact.title) == (
        "MERGED",
        "2026-09-22T11:00:00Z",
        "a" * 40,
        "fix(OMN-1): work",
    )
    assert seen == [
        [
            "gh",
            "pr",
            "view",
            "11",
            "--repo",
            "Example-Org/omnimarket",
            "--json",
            "state,mergedAt,mergeCommit,title",
        ]
    ]


@pytest.mark.parametrize("failure", ["nonzero", "badjson", "timeout", "missing-tool"])
def test_every_pr_lookup_failure_is_a_lookup_failed_fact(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fake(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if failure == "missing-tool":
            raise FileNotFoundError("gh")
        return _completed(1) if failure == "nonzero" else _completed(0, "not json")

    monkeypatch.setattr(adapters, "_run", fake)
    fact = GhPullRequests().pr("Example-Org", "omnimarket", 11)
    assert fact.state == "LOOKUP_FAILED"
    assert fact.merged_at == ""
    assert fact.merge_sha == ""


def test_an_open_pr_has_no_merge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapters,
        "_run",
        lambda *_args, **_kwargs: _completed(
            0,
            json.dumps(
                {"state": "OPEN", "mergedAt": None, "mergeCommit": None, "title": "t"}
            ),
        ),
    )
    fact = GhPullRequests().pr("Example-Org", "omnimarket", 12)
    assert (fact.state, fact.merged_at, fact.merge_sha) == ("OPEN", "", "")


def test_the_push_time_is_the_last_commit_date_or_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adapters,
        "_run",
        lambda *_args, **_kwargs: _completed(0, "2026-09-22T19:45:00Z\n"),
    )
    assert (
        GhPullRequests().pushed_at("O", "omnimarket", 12).committed_at
        == "2026-09-22T19:45:00Z"
    )
    monkeypatch.setattr(adapters, "_run", lambda *_args, **_kwargs: _completed(1))
    assert GhPullRequests().pushed_at("O", "omnimarket", 12).committed_at == ""

    def boom(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(adapters, "_run", boom)
    assert GhPullRequests().pushed_at("O", "omnimarket", 12).committed_at == ""


# --- git ------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """A registry root holding one clone with a landed commit and a commit on an unmerged branch."""
    root = tmp_path / "registry_root"
    clone = root / "omnimarket"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    (clone / "a.txt").write_text("a")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-q", "-m", "feat(OMN-7): landed work\n\nbody mentions OMN-8")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(clone, "switch", "-q", "-c", "side")
    (clone / "b.txt").write_text("b")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-q", "-m", "wip")
    _git(clone, "switch", "-q", "main")
    return root


def test_a_landed_commit_is_found_with_its_time_and_tickets(registry: Path) -> None:
    clone = registry / "omnimarket"
    sha = _git(clone, "rev-parse", "main")
    fact = GitCommits().probe(
        registry,
        "registry_root",
        ModelShaRef(sha=sha, candidates=("registry_root", "omnimarket")),
    )
    assert fact.found_in == "omnimarket"
    assert fact.landed
    assert fact.committer_at.endswith("+00:00")
    assert fact.msg_tickets == ("OMN-7", "OMN-8")


def test_a_commit_off_the_landing_refs_is_found_but_not_landed(registry: Path) -> None:
    sha = _git(registry / "omnimarket", "rev-parse", "side")
    fact = GitCommits().probe(
        registry, "registry_root", ModelShaRef(sha=sha, candidates=("omnimarket",))
    )
    assert fact.found_in == "omnimarket"
    assert not fact.landed


def test_a_commit_in_no_candidate_is_not_found(registry: Path) -> None:
    fact = GitCommits().probe(
        registry,
        "registry_root",
        ModelShaRef(
            sha="deadbeefdeadbeef", candidates=("registry_root", "omnimarket", "absent")
        ),
    )
    assert fact.found_in == ""
    assert not fact.landed


# --- ledger writer -----------------------------------------------------------------


def _writer(tmp_path: Path, script: str) -> str:
    """The command that runs ``script`` as the ledger writer (interpreted, never exec'd)."""
    path = tmp_path / "writer.py"
    path.write_text(script)
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


def test_a_row_is_appended_by_the_ledger_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "argv.json"
    writer = _writer(
        tmp_path,
        f"import json, sys\nopen({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
    )
    monkeypatch.setenv(adapters.APPEND_COMMAND_ENV, writer)
    assert LedgerWriterAppender().append(tmp_path / "ledger.md", "the row") == ""
    assert json.loads(out.read_text()) == [
        str(tmp_path / "ledger.md"),
        "--append",
        "the row",
    ]


def test_contention_is_retried_then_given_up_with_the_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(tmp_path, "import sys\nsys.exit(75)\n")
    monkeypatch.setenv(adapters.APPEND_COMMAND_ENV, writer)
    sleeps: list[float] = []
    monkeypatch.setattr(adapters.time, "sleep", sleeps.append)
    error = LedgerWriterAppender().append(tmp_path / "ledger.md", "row")
    assert error == "exit 75 (contention) after 5 attempts"
    assert sleeps == [2.0, 4.0, 6.0, 8.0]


def test_contention_that_clears_is_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "count"
    writer = _writer(
        tmp_path,
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "sys.exit(75 if n < 3 else 0)\n",
    )
    monkeypatch.setenv(adapters.APPEND_COMMAND_ENV, writer)
    monkeypatch.setattr(adapters.time, "sleep", lambda *_args: None)
    assert LedgerWriterAppender().append(tmp_path / "ledger.md", "row") == ""
    assert counter.read_text() == "3"


def test_a_refusal_is_returned_with_its_exit_code_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _writer(
        tmp_path,
        "import sys\nsys.stderr.write('row refused: bad grammar\\n')\nsys.exit(2)\n",
    )
    monkeypatch.setenv(adapters.APPEND_COMMAND_ENV, writer)
    assert (
        LedgerWriterAppender().append(tmp_path / "ledger.md", "row")
        == "ledger_lock exit 2: row refused: bad grammar"
    )


def test_a_writer_that_cannot_start_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(adapters.APPEND_COMMAND_ENV, str(tmp_path / "no-such-writer"))
    error = LedgerWriterAppender().append(tmp_path / "ledger.md", "row")
    assert error.startswith("ledger writer did not run: FileNotFoundError")
