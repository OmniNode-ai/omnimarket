# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm`` clone-only reads: pr-commits, file-read, commit-view, commit-checks (OMN-20913).

The source-control gateway declares no operation for these, so each is answered
from a real git clone and a fixture watcher state, and a read the local sources
cannot answer exits 3 with its reason. A bus that fails the test when it is
opened stands in for "no publish, no GitHub request".
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest
from click.testing import CliRunner, Result
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.handler_scm_dispatch import (
    SCM_LOCAL_VERBS,
    SCM_REFUSED_VERBS,
    SCM_VERBS,
)
from omnimarket.handlers.scm.handler_scm_local_git import COMMIT_LIST_LIMIT

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
BINARY = bytes(range(256))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **scrub_git_location_env(os.environ),
            "GIT_AUTHOR_NAME": "Ada",
            "GIT_AUTHOR_EMAIL": "ada@example.invalid",
            "GIT_COMMITTER_NAME": "Ada",
            "GIT_COMMITTER_EMAIL": "ada@example.invalid",
        },
    ).stdout.strip()


class Repo:
    """A bare origin, the canonical clone of it, and the commits they share."""

    def __init__(self, root: Path) -> None:
        self.root = root
        origin = root / "origin.git"
        work = root / "work"
        work.mkdir()
        _git(root, "init", "-q", "--bare", str(origin))
        _git(work, "init", "-q", "-b", "dev")
        (work / "README.md").write_text("# readme\nline two\n", encoding="utf-8")
        (work / "src").mkdir()
        (work / "src" / "app.py").write_text("print('dev')\n", encoding="utf-8")
        (work / "src" / "blob.bin").write_bytes(BINARY)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "feat: root")
        self.root_sha = _git(work, "rev-parse", "HEAD")
        _git(work, "tag", "v1")
        (work / "src" / "app.py").write_text("print('dev two')\n", encoding="utf-8")
        _git(work, "commit", "-q", "-am", "fix: second\n\nbody line")
        self.dev_sha = _git(work, "rev-parse", "HEAD")
        _git(work, "checkout", "-q", "-b", "jonah/omn-1-work")
        (work / "NEW.md").write_text("new\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "work one")
        self.first = _git(work, "rev-parse", "HEAD")
        (work / "src" / "app.py").write_text("print('pr')\n", encoding="utf-8")
        _git(work, "commit", "-q", "-am", "work two")
        self.pr_head = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "-q", str(origin), "dev", "jonah/omn-1-work", "v1")
        self.clone = root / "omnimarket"
        _git(root, "clone", "-q", "-b", "dev", str(origin), str(self.clone))
        _git(
            self.clone,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/dev",
        )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    built = Repo(tmp_path)
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(tmp_path))

    def refuse(_: dict[str, object]) -> NoReturn:
        raise AssertionError("a clone-only read opened the bus")

    monkeypatch.setattr(scm_cli, "_open_bus", refuse)
    return built


def _watcher(
    tmp_path: Path,
    *,
    head: str,
    state: str = "OPEN",
    age: timedelta = timedelta(minutes=1),
) -> Path:
    tick = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "schema": 1,
        "last_tick": tick,
        "prs": {
            "omnimarket#77": {
                "cls": "pending",
                "ci": {
                    "read_at": tick,
                    "red": [],
                    "pending": ["CI Summary"],
                    "cancelled": [],
                    "red_facts": [],
                    "runs": [],
                    "detail": [["CI Summary", "9001", tick]],
                },
                "facts": {
                    "number": 77,
                    "state": state,
                    "base": "dev",
                    "head_ref": "jonah/omn-1-work",
                    "head_sha": head,
                },
            }
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _run(args: list[str]) -> Result:
    return CliRunner().invoke(scm_cli.scm_group, args, catch_exceptions=False)


def _answer(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    loaded: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return loaded


# ------------------------------------------------------------ the verb table


def test_clone_only_verbs_are_distinct_from_node_verbs_and_refusals() -> None:
    local = {v.verb for v in SCM_LOCAL_VERBS}
    assert local == {"pr-commits", "file-read", "commit-view", "commit-checks"}
    assert local.isdisjoint({v.verb for v in SCM_VERBS})
    assert local.isdisjoint({v.verb for v in SCM_REFUSED_VERBS})
    assert len({v.read for v in SCM_LOCAL_VERBS}) == len(SCM_LOCAL_VERBS)


def test_verbs_lists_the_clone_only_verbs_with_the_gh_read_they_replace() -> None:
    table = json.loads(_run(["verbs"]).stdout)
    listed = {v["verb"]: v["replaces"] for v in table["served_local_only"]}
    assert listed["pr-commits"] == "gh api repos/<o>/<r>/pulls/<n>/commits"
    assert listed["file-read"] == "gh api repos/<o>/<r>/contents/<path>"
    assert listed["commit-view"] == "gh api repos/<o>/<r>/commits/<ref>"
    assert listed["commit-checks"] == "gh api repos/<o>/<r>/commits/<sha>/check-runs"


# --------------------------------------------------------------- pr-commits


def test_pr_commits_lists_the_range_oldest_first_from_the_clone(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head)
    answer = _answer(_run(["pr-commits", f"{REPO}#77", "--watcher-state", str(state)]))
    assert answer["source"] == "clone"
    assert answer["verb"] == "pr-commits"
    assert answer["watcher_tick"] is not None
    result = answer["result"]
    assert [c["sha"] for c in result["commits"]] == [repo.first, repo.pr_head]
    assert [c["subject"] for c in result["commits"]] == ["work one", "work two"]
    assert result["count"] == 2
    assert result["truncated"] is False
    assert result["base"] == "dev"
    assert result["commits"][1]["parents"] == [repo.first]
    assert result["commits"][0]["author"] == "Ada"


def test_pr_commits_needs_a_pr_number(repo: Repo) -> None:
    result = CliRunner().invoke(scm_cli.scm_group, ["pr-commits", REPO])
    assert result.exit_code == scm_cli.EXIT_USAGE
    assert "OWNER/REPO#NUMBER" in result.output


def test_pr_commits_with_a_stale_watcher_is_unanswered_not_read_from_github(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head, age=timedelta(minutes=30))
    result = _run(["pr-commits", f"{REPO}#77", "--watcher-state", str(state)])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "older than" in result.stderr
    assert "no bus or GitHub fallback" in result.stderr


def test_pr_commits_of_a_merged_pr_is_answered_from_a_stale_watcher(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head, state="MERGED", age=timedelta(days=2))
    answer = _answer(_run(["pr-commits", f"{REPO}#77", "--watcher-state", str(state)]))
    assert answer["result"]["count"] == 2


def test_pr_commits_whose_head_the_clone_lacks_says_so(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head="d" * 40)
    result = _run(["pr-commits", f"{REPO}#77", "--watcher-state", str(state)])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "does not hold both origin/dev" in result.stderr


def test_pr_commits_for_a_pr_the_watcher_does_not_know_is_unanswered(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head)
    result = _run(["pr-commits", f"{REPO}#99", "--watcher-state", str(state)])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "no record of omnimarket#99" in result.stderr


def test_pr_commits_is_capped_and_says_it_was_truncated(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnimarket.handlers.scm.handler_scm_local_git.COMMIT_LIST_LIMIT", 1
    )
    state = _watcher(tmp_path, head=repo.pr_head)
    result = _answer(_run(["pr-commits", f"{REPO}#77", "--watcher-state", str(state)]))[
        "result"
    ]
    assert result["truncated"] is True
    assert result["count"] == 1
    assert len(result["commits"]) == 1
    assert COMMIT_LIST_LIMIT > 1


# ---------------------------------------------------------------- file-read


def test_file_read_defaults_to_the_clone_default_branch(repo: Repo) -> None:
    answer = _answer(_run(["file-read", REPO, "--path", "src/app.py"]))
    assert answer["source"] == "clone"
    assert answer["watcher_tick"] is None
    result = answer["result"]
    assert result["content"] == "print('dev two')\n"
    assert result["encoding"] == "utf-8"
    assert result["type"] == "blob"
    assert result["commit"] == repo.dev_sha
    assert result["truncated"] is False


def test_file_read_resolves_a_branch_a_tag_and_a_commit(repo: Repo) -> None:
    def content(ref: str) -> str:
        out = _answer(_run(["file-read", REPO, "--path", "src/app.py", "--ref", ref]))
        return str(out["result"]["content"])

    assert content("jonah/omn-1-work") == "print('pr')\n"
    assert content("origin/jonah/omn-1-work") == "print('pr')\n"
    assert content("v1") == "print('dev')\n"
    assert content(repo.root_sha) == "print('dev')\n"
    assert content(repo.root_sha[:10]) == "print('dev')\n"


def test_file_read_returns_a_binary_file_as_base64(repo: Repo) -> None:
    result = _answer(_run(["file-read", REPO, "--path", "src/blob.bin"]))["result"]
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == BINARY
    assert result["total_bytes"] == 256


def test_file_read_truncates_at_max_bytes(repo: Repo) -> None:
    result = _answer(
        _run(["file-read", REPO, "--path", "README.md", "--max-bytes", "4"])
    )["result"]
    assert result["content"] == "# re"
    assert result["truncated"] is True
    assert result["total_bytes"] == len("# readme\nline two\n")


def test_file_read_lists_a_directory(repo: Repo) -> None:
    result = _answer(_run(["file-read", REPO, "--path", "src"]))["result"]
    assert result["type"] == "tree"
    entries = {e["name"]: e for e in result["entries"]}
    assert set(entries) == {"app.py", "blob.bin"}
    assert entries["blob.bin"]["size"] == 256
    assert entries["app.py"]["type"] == "blob"


@pytest.mark.parametrize("path", ["missing.txt", "src/missing.py"])
def test_file_read_of_a_path_not_in_the_tree_is_unanswered(
    repo: Repo, path: str
) -> None:
    result = _run(["file-read", REPO, "--path", path])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "is not in the tree" in result.stderr


def test_file_read_of_an_unknown_ref_is_unanswered(repo: Repo) -> None:
    result = _run(["file-read", REPO, "--path", "README.md", "--ref", "nope"])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "holds no commit for ref nope" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--path", "../outside"],
        ["--path", "src/../../outside"],
        ["--path", "/etc/passwd"],
        ["--path", "README.md", "--ref", "--output=/tmp/x"],
        ["--path", "README.md", "--ref", "dev..jonah/omn-1-work"],
        ["--path", "README.md", "--ref", "-x"],
    ],
)
def test_file_read_refuses_a_path_or_ref_that_could_leave_the_tree(
    repo: Repo, args: list[str]
) -> None:
    result = CliRunner().invoke(scm_cli.scm_group, ["file-read", REPO, *args])
    assert result.exit_code == scm_cli.EXIT_USAGE, result.output


def test_file_read_without_a_path_is_a_usage_error(repo: Repo) -> None:
    result = CliRunner().invoke(scm_cli.scm_group, ["file-read", REPO])
    assert result.exit_code == scm_cli.EXIT_USAGE
    assert "--path" in result.output


def test_file_read_without_a_clone_says_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(tmp_path))
    result = _run(["file-read", REPO, "--path", "README.md"])
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "no canonical clone of OmniNode-ai/omnimarket" in result.stderr


# -------------------------------------------------------------- commit-view


def test_commit_view_by_sha_names_message_parents_and_files(repo: Repo) -> None:
    result = _answer(_run(["commit-view", REPO, "--ref", repo.dev_sha]))["result"]
    assert result["sha"] == repo.dev_sha
    assert result["parents"] == [repo.root_sha]
    assert result["author"] == "Ada"
    assert result["message"] == "fix: second\n\nbody line"
    assert result["files"] == [{"status": "M", "path": "src/app.py"}]


def test_commit_view_by_branch_and_of_the_root_commit(repo: Repo) -> None:
    by_branch = _answer(_run(["commit-view", REPO, "--ref", "jonah/omn-1-work"]))
    assert by_branch["result"]["sha"] == repo.pr_head
    root = _answer(_run(["commit-view", REPO, "--ref", "v1"]))["result"]
    assert root["parents"] == []
    assert {f["path"] for f in root["files"]} == {
        "README.md",
        "src/app.py",
        "src/blob.bin",
    }
    assert {f["status"] for f in root["files"]} == {"A"}


def test_commit_view_needs_a_ref_and_a_known_commit(repo: Repo) -> None:
    missing = CliRunner().invoke(scm_cli.scm_group, ["commit-view", REPO])
    assert missing.exit_code == scm_cli.EXIT_USAGE
    assert "--ref" in missing.output
    unknown = _run(["commit-view", REPO, "--ref", "e" * 40])
    assert unknown.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED


# ----------------------------------------------------------- commit-checks


def test_commit_checks_answers_from_the_watcher_by_head_sha(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head)
    answer = _answer(
        _run(
            [
                "commit-checks",
                REPO,
                "--sha",
                repo.pr_head,
                "--watcher-state",
                str(state),
            ]
        )
    )
    assert answer["source"] == "watcher"
    result = answer["result"]
    assert result["number"] == 77
    assert result["pending"] == ["CI Summary"]
    assert result["head_sha"] == repo.pr_head
    assert result["detail"][0][:2] == ["CI Summary", "9001"]


def test_commit_checks_accepts_an_abbreviated_sha(repo: Repo, tmp_path: Path) -> None:
    state = _watcher(tmp_path, head=repo.pr_head)
    result = _answer(
        _run(
            [
                "commit-checks",
                REPO,
                "--sha",
                repo.pr_head[:9],
                "--watcher-state",
                str(state),
            ]
        )
    )["result"]
    assert result["number"] == 77


def test_commit_checks_of_a_commit_no_watched_pr_has_is_unanswered(
    repo: Repo, tmp_path: Path
) -> None:
    state = _watcher(tmp_path, head=repo.pr_head)
    result = _run(
        ["commit-checks", REPO, "--sha", repo.dev_sha, "--watcher-state", str(state)]
    )
    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    assert "holds no CI read of a OmniNode-ai/omnimarket PR" in result.stderr


def test_commit_checks_with_a_stale_watcher_is_unanswered_unless_terminal(
    repo: Repo, tmp_path: Path
) -> None:
    stale = _watcher(tmp_path, head=repo.pr_head, age=timedelta(minutes=30))
    args = ["commit-checks", REPO, "--sha", repo.pr_head, "--watcher-state"]
    assert _run([*args, str(stale)]).exit_code == scm_cli.EXIT_LOCAL_UNANSWERED
    merged = _watcher(
        tmp_path, head=repo.pr_head, state="MERGED", age=timedelta(days=2)
    )
    assert _answer(_run([*args, str(merged)]))["result"]["number"] == 77


def test_commit_checks_rejects_a_sha_that_is_not_hex(repo: Repo) -> None:
    result = CliRunner().invoke(
        scm_cli.scm_group, ["commit-checks", REPO, "--sha", "main"]
    )
    assert result.exit_code == scm_cli.EXIT_USAGE


def test_positive_control_a_node_verb_does_open_the_bus(repo: Repo) -> None:
    with pytest.raises(AssertionError, match="opened the bus"):
        _run(["pr-list", REPO, "--source", "bus"])
