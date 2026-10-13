# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watch reads checks over the GitHub REST API, not `gh pr checks` (OMN-21033).

The deployed effects runtime ships gh 2.46, whose `gh pr checks` has no `--json`
flag, so the CLI client could only ever return "unknown flag: --json". The
client under test reads the PR head, its check runs and its commit statuses
through the shared GitHub REST helper and returns the same typed snapshot.
"""

from __future__ import annotations

import pytest

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator import (
    HandlerPrChecksGithubRest,
    PrChecksClientError,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models import (
    EnumPrCheckBucket,
    ModelPrWatchOrchestratorRequest,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
SHA = "d8f51e613d3a3353cbdb063e1f635105b44a8146"
TOKEN = "ghp_not_a_real_token_value"


def _request(*, required_only: bool = False) -> ModelPrWatchOrchestratorRequest:
    return ModelPrWatchOrchestratorRequest(
        repo=REPO, pr_number=3459, required_only=required_only
    )


def _run(name: str, status: str, conclusion: str | None) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.test/{name}",
        "started_at": "2026-10-12T10:00:00Z",
        "completed_at": "2026-10-12T10:01:00Z" if status == "completed" else None,
        "app": {"slug": "github-actions"},
    }


class _FakeRest:
    """Serves the three endpoints the client reads and records every path."""

    def __init__(
        self,
        runs: list[dict[str, object]],
        statuses: list[dict[str, object]] | None = None,
        *,
        page_size: int = 100,
    ) -> None:
        self.runs = runs
        self.statuses = statuses or []
        self.page_size = page_size
        self.paths: list[str] = []
        self.tokens: list[str] = []

    def __call__(self, path: str, token: str) -> dict[str, object]:
        self.paths.append(path)
        self.tokens.append(token)
        if path == f"/repos/{REPO}/pulls/3459":
            return {"head": {"sha": SHA}}
        if path.startswith(f"/repos/{REPO}/commits/{SHA}/check-runs"):
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * self.page_size
            return {
                "total_count": len(self.runs),
                "check_runs": self.runs[start : start + self.page_size],
            }
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return {"statuses": self.statuses}
        raise AssertionError(f"unexpected GitHub read: {path}")


def _client(rest: _FakeRest) -> HandlerPrChecksGithubRest:
    return HandlerPrChecksGithubRest(rest_get=rest, token_source=lambda: TOKEN)


@pytest.mark.parametrize(
    ("status", "conclusion", "bucket"),
    [
        ("completed", "success", EnumPrCheckBucket.PASS),
        ("completed", "skipped", EnumPrCheckBucket.SKIPPING),
        ("completed", "neutral", EnumPrCheckBucket.SKIPPING),
        ("completed", "failure", EnumPrCheckBucket.FAIL),
        ("completed", "timed_out", EnumPrCheckBucket.FAIL),
        ("completed", "action_required", EnumPrCheckBucket.FAIL),
        ("completed", "startup_failure", EnumPrCheckBucket.FAIL),
        ("completed", "cancelled", EnumPrCheckBucket.CANCEL),
        ("in_progress", None, EnumPrCheckBucket.PENDING),
        ("queued", None, EnumPrCheckBucket.PENDING),
        ("waiting", None, EnumPrCheckBucket.PENDING),
        ("completed", "an_unheard_of_conclusion", EnumPrCheckBucket.UNKNOWN),
    ],
)
def test_pr_watch_rest_client_maps_a_check_run_to_a_bucket(
    status: str, conclusion: str | None, bucket: EnumPrCheckBucket
) -> None:
    checks = _client(_FakeRest([_run("unit", status, conclusion)])).fetch_checks(
        _request()
    )

    assert [(c.name, c.bucket) for c in checks] == [("unit", bucket)]
    assert checks[0].link == "https://github.test/unit"


@pytest.mark.parametrize(
    ("state", "bucket"),
    [
        ("success", EnumPrCheckBucket.PASS),
        ("pending", EnumPrCheckBucket.PENDING),
        ("failure", EnumPrCheckBucket.FAIL),
        ("error", EnumPrCheckBucket.FAIL),
    ],
)
def test_pr_watch_rest_client_maps_a_commit_status_to_a_bucket(
    state: str, bucket: EnumPrCheckBucket
) -> None:
    status: dict[str, object] = {
        "context": "ci/legacy",
        "state": state,
        "target_url": "https://x.test",
    }

    checks = _client(_FakeRest([], [status])).fetch_checks(_request())

    assert [(c.name, c.bucket) for c in checks] == [("ci/legacy", bucket)]


def test_pr_watch_rest_client_reads_every_page_of_check_runs() -> None:
    runs = [_run(f"job-{i}", "completed", "success") for i in range(5)]
    rest = _FakeRest(runs, page_size=2)

    checks = _client(rest).fetch_checks(_request())

    assert [c.name for c in checks] == [f"job-{i}" for i in range(5)]
    pages = [p for p in rest.paths if "check-runs" in p]
    assert len(pages) == 3, f"expected 3 pages of check runs, read {pages}"


def test_pr_watch_rest_client_sends_the_resolved_token_and_only_reads() -> None:
    rest = _FakeRest([_run("unit", "completed", "success")])

    _client(rest).fetch_checks(_request())

    assert set(rest.tokens) == {TOKEN}
    assert all(path.startswith("/repos/") for path in rest.paths)


def test_pr_watch_rest_client_turns_a_github_error_into_a_typed_refusal() -> None:
    def refuse(path: str, token: str) -> dict[str, object]:
        raise GitHubApiError(f"Not Found for {path}", status_code=404)

    client = HandlerPrChecksGithubRest(rest_get=refuse, token_source=lambda: TOKEN)

    with pytest.raises(PrChecksClientError, match="404") as caught:
        client.fetch_checks(_request())
    assert TOKEN not in str(caught.value)


def test_pr_watch_rest_client_refuses_required_only_rather_than_reading_all_checks() -> (
    None
):
    rest = _FakeRest([_run("unit", "completed", "failure")])

    with pytest.raises(PrChecksClientError, match="required_only"):
        _client(rest).fetch_checks(_request(required_only=True))

    assert rest.paths == [], "a refused request must not touch GitHub"
