# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LocalGitHubPrReads answers triage's PR reads from the watcher state and clones."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
    LinearClientProtocol,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageStartCommand,
)
from omnimarket.nodes.node_linear_triage.services.local_github_pr_reads import (
    LocalGitHubPrReads,
    LocalGitHubStateError,
)


def _git(clone: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone_with_squash_merges(tmp_path: Path) -> Path:
    clone = tmp_path / "widgets"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "dev")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    for subject, body in (
        ("feat(ABC-12): add widget (#7)", "Closes ABC-12\n"),
        ("chore: unrelated (#8)", "mentions ABC-123 only\n"),
        ("direct push without a PR number", ""),
    ):
        _git(clone, "commit", "-q", "--allow-empty", "-m", f"{subject}\n\n{body}")
    _git(clone, "update-ref", "refs/remotes/origin/dev", "HEAD")
    _git(
        clone,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/dev",
    )
    return clone


def _state_file(tmp_path: Path) -> Path:
    def rec(repo_num: str, **facts: str) -> dict[str, object]:
        return {"facts": facts}

    state = {
        "last_tick": "2026-10-17T17:49:07Z",
        "repos": {"widgets": {}},
        "prs": {
            "widgets#9": rec(
                "widgets#9",
                state="MERGED",
                title="fix(ABC-12): second part",
                body="",
                merged_at="2026-10-16T01:00:00Z",
                head_ref="jo/abc-12-widget",
            ),
            "widgets#10": rec(
                "widgets#10",
                state="CLOSED",
                title="feat(ABC-12): first try",
                body="",
                head_ref="jo/abc-12-first",
            ),
            "widgets#11": rec(
                "widgets#11",
                state="OPEN",
                title="feat(ABC-12): third part",
                body="",
                head_ref="jo/abc-12-open",
            ),
            "widgets#12": rec(
                "widgets#12",
                state="MERGED",
                title="no merge time recorded ABC-12",
                body="",
            ),
        },
        "merges": {},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


@pytest.fixture
def client(tmp_path: Path) -> LocalGitHubPrReads:
    clone = _clone_with_squash_merges(tmp_path)
    return LocalGitHubPrReads(
        watcher_state_path=_state_file(tmp_path),
        clone_for=lambda slug: clone if slug.endswith("/widgets") else None,
    )


@pytest.mark.unit
def test_merged_search_joins_clone_squash_commits_and_watcher_records(
    client: LocalGitHubPrReads,
) -> None:
    hits = client.search_prs_in_repo(
        repo="widgets", search_term="ABC-12", state="merged"
    )
    assert sorted(p["number"] for p in hits) == ["7", "9"]
    squash = next(p for p in hits if p["number"] == "7")
    assert squash["title"] == "feat(ABC-12): add widget"
    assert "Closes ABC-12" in squash["body"]
    assert squash["mergedAt"]


@pytest.mark.unit
def test_search_term_does_not_match_a_longer_id(client: LocalGitHubPrReads) -> None:
    numbers = {
        p["number"] for p in client.search_prs(search_term="ABC-12", state="all")
    }
    assert "8" not in numbers
    only_long = client.search_prs(search_term="ABC-123", state="merged")
    assert [p["number"] for p in only_long] == ["8"]


@pytest.mark.unit
def test_closed_state_means_closed_unmerged_and_open_is_open(
    client: LocalGitHubPrReads,
) -> None:
    closed = client.search_prs_in_repo(
        repo="widgets", search_term="ABC-12", state="closed"
    )
    assert [p["number"] for p in closed] == ["10"]
    assert not closed[0]["mergedAt"]
    opened = client.search_prs(search_term="ABC-12", state="open")
    assert [p["number"] for p in opened] == ["11"]


@pytest.mark.unit
def test_merged_record_without_merge_time_is_skipped(
    client: LocalGitHubPrReads,
) -> None:
    numbers = {
        p["number"] for p in client.search_prs(search_term="ABC-12", state="all")
    }
    assert "12" not in numbers


@pytest.mark.unit
def test_list_prs_by_head_and_closing_refs(client: LocalGitHubPrReads) -> None:
    merged = client.list_prs_by_head(repo="widgets", branch="jo/abc-12-widget")
    assert [p["number"] for p in merged] == ["9"]
    assert client.list_prs_by_head(repo="widgets", branch="jo/abc-12-open") == []
    assert client.pr_closing_ticket_refs(repo="widgets", number=9) == []


@pytest.mark.unit
def test_repo_without_a_clone_is_reported_not_silent(tmp_path: Path) -> None:
    gh = LocalGitHubPrReads(
        watcher_state_path=_state_file(tmp_path), clone_for=lambda _slug: None
    )
    hits = gh.search_prs_in_repo(repo="widgets", search_term="ABC-12", state="merged")
    assert [p["number"] for p in hits] == ["9"]
    assert gh.unread_repos == {"widgets"}


@pytest.mark.unit
def test_missing_watcher_state_raises(tmp_path: Path) -> None:
    with pytest.raises(LocalGitHubStateError):
        LocalGitHubPrReads(watcher_state_path=tmp_path / "absent.json")


def _empty_linear() -> Any:
    linear = MagicMock(spec=LinearClientProtocol)
    linear.list_issues.return_value = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }
        }
    }
    linear.list_children.return_value = {"data": {"issues": {"nodes": []}}}
    return linear


@pytest.mark.unit
async def test_handler_local_source_needs_no_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(_state_file(tmp_path)))
    result = await HandlerLinearTriage(client=_empty_linear()).handle(
        ModelLinearTriageStartCommand(
            project_id="p-1", github_source="local", dry_run=True
        )
    )
    assert result.status == "completed"
    assert result.validation_errors == []


@pytest.mark.unit
async def test_handler_local_source_without_watcher_state_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(tmp_path / "absent.json"))
    result = await HandlerLinearTriage(client=_empty_linear()).handle(
        ModelLinearTriageStartCommand(
            project_id="p-1", github_source="local", dry_run=True
        )
    )
    assert result.status == "error"
    assert "watcher" in result.validation_errors[0]
