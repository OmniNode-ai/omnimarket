# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR triage reads on the one omnimarket GitHub transport (OMN-20915).

The three triage reads the PR poller needs (open PR list, combined commit
status, latest review states) fold into ``GitHubHttpTransport`` so no second
GitHub client is needed. The error posture is the one the poller has always
had: a failed PR list is an error, a failed per-PR read degrades to the
neutral value.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket import github_api
from omnimarket.github_api import GitHubApiError, GitHubHttpTransport


def _transport() -> GitHubHttpTransport:
    return GitHubHttpTransport("test-token")


@pytest.mark.unit
class TestFetchCombinedStatus:
    def test_empty_sha_is_pending_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise AssertionError("no request expected for an empty sha")

        monkeypatch.setattr(github_api, "rest_json", _boom)
        assert _transport().fetch_combined_status("o/r", "") == "pending"

    def test_returns_the_state_of_the_status_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def _fake(method: str, path: str, **_k: Any) -> dict[str, Any]:
            seen.append(f"{method} {path}")
            return {"state": "success"}

        monkeypatch.setattr(github_api, "rest_json", _fake)
        assert _transport().fetch_combined_status("o/r", "abc123") == "success"
        assert seen == ["GET /repos/o/r/commits/abc123/status"]

    def test_a_failed_read_degrades_to_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise GitHubApiError("boom", status_code=500)

        monkeypatch.setattr(github_api, "rest_json", _fail)
        assert _transport().fetch_combined_status("o/r", "abc123") == "pending"


@pytest.mark.unit
class TestFetchReviewStates:
    def test_keeps_the_latest_decisive_state_per_reviewer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reviews = [
            {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "a"}, "state": "APPROVED"},
            {"user": {"login": "b"}, "state": "COMMENTED"},
            {"user": {"login": "c"}, "state": "CHANGES_REQUESTED"},
        ]
        monkeypatch.setattr(github_api, "rest_json_array", lambda *_a, **_k: reviews)
        assert sorted(_transport().fetch_review_states("o/r", 7)) == [
            "APPROVED",
            "CHANGES_REQUESTED",
        ]

    def test_a_failed_read_degrades_to_no_states(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise GitHubApiError("boom", status_code=502)

        monkeypatch.setattr(github_api, "rest_json_array", _fail)
        assert _transport().fetch_review_states("o/r", 7) == []


@pytest.mark.unit
class TestFetchOpenPrsForTriage:
    def test_augments_each_pr_with_status_and_reviews(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _array(method: str, path: str, **_k: Any) -> list[dict[str, Any]]:
            if path.startswith("/repos/o/r/pulls?state=open"):
                return [{"number": 1, "head": {"sha": "s1"}}]
            if path == "/repos/o/r/pulls/1/reviews":
                return [{"user": {"login": "a"}, "state": "APPROVED"}]
            raise AssertionError(f"unexpected {method} {path}")

        monkeypatch.setattr(github_api, "rest_json_array", _array)
        monkeypatch.setattr(
            github_api, "rest_json", lambda *_a, **_k: {"state": "failure"}
        )
        prs = _transport().fetch_open_prs_for_triage("o/r")
        assert prs == [
            {
                "number": 1,
                "head": {"sha": "s1"},
                "combined_status": "failure",
                "review_states": ["APPROVED"],
            }
        ]

    def test_pages_until_a_short_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages: list[str] = []

        def _array(method: str, path: str, **_k: Any) -> list[dict[str, Any]]:
            if "/reviews" in path:
                return []
            pages.append(path)
            if path.endswith("page=1"):
                return [{"number": n, "head": {"sha": "x"}} for n in range(100)]
            return [{"number": 100, "head": {"sha": "x"}}]

        monkeypatch.setattr(github_api, "rest_json_array", _array)
        monkeypatch.setattr(
            github_api, "rest_json", lambda *_a, **_k: {"state": "success"}
        )
        prs = _transport().fetch_open_prs_for_triage("o/r")
        assert len(prs) == 101
        assert pages == [
            "/repos/o/r/pulls?state=open&per_page=100&page=1",
            "/repos/o/r/pulls?state=open&per_page=100&page=2",
        ]

    def test_a_failed_pr_list_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise GitHubApiError("boom", status_code=500)

        monkeypatch.setattr(github_api, "rest_json_array", _fail)
        with pytest.raises(GitHubApiError):
            _transport().fetch_open_prs_for_triage("o/r")
