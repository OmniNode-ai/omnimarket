# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
    GitHubCliAdapter,
    _run_id_from_details_url,
)


@pytest.mark.unit
class TestGitHubCliAdapter:
    async def test_rerun_failed_checks_enumerates_and_reruns_each(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graphql_calls: list[tuple[str, dict[str, object]]] = []
        rerun_calls: list[tuple[str, str]] = []

        def fake_graphql(
            query: str, variables: dict[str, object], **kwargs: object
        ) -> dict[str, object]:
            graphql_calls.append((query, variables))
            return {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "conclusion": "FAILURE",
                                                        "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/111/job/1",
                                                    },
                                                    {
                                                        "__typename": "CheckRun",
                                                        "conclusion": "SUCCESS",
                                                        "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/222/job/2",
                                                    },
                                                    {
                                                        "__typename": "CheckRun",
                                                        "conclusion": "TIMED_OUT",
                                                        "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/333/job/3",
                                                    },
                                                    {
                                                        "__typename": "CheckRun",
                                                        "conclusion": "FAILURE",
                                                        "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/111/job/4",
                                                    },
                                                ]
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }

        def fake_rest_no_content(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> None:
            del body, token
            rerun_calls.append((method, path))

        async def fake_resolve_token_async() -> str:
            return "fake-token"

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.graphql",
            fake_graphql,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_no_content",
            fake_rest_no_content,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token_async",
            fake_resolve_token_async,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token",
            lambda: "fake-token",
        )

        adapter = GitHubCliAdapter()
        result = await adapter.rerun_failed_checks("OmniNode-ai/omnimarket", 42)

        assert "rerequested 2 failed run(s)" in result
        assert len(graphql_calls) == 1
        assert rerun_calls == [
            (
                "POST",
                "/repos/OmniNode-ai/omnimarket/actions/runs/111/rerun-failed-jobs",
            ),
            (
                "POST",
                "/repos/OmniNode-ai/omnimarket/actions/runs/333/rerun-failed-jobs",
            ),
        ]

    async def test_rerun_failed_checks_no_failed_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_graphql(
            query: str, variables: dict[str, object], **kwargs: object
        ) -> dict[str, object]:
            del query, variables
            return {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {"contexts": {"nodes": []}}
                                    }
                                }
                            ]
                        }
                    }
                }
            }

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.graphql",
            fake_graphql,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token",
            lambda: "fake-token",
        )

        adapter = GitHubCliAdapter()
        result = await adapter.rerun_failed_checks("OmniNode-ai/omnimarket", 42)

        assert "no failed checks" in result

    async def test_resolve_conflicts_calls_update_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_rest_json(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> dict[str, object]:
            calls.append((method, path, body))
            if method == "GET":
                return {"head": {"sha": "deadbeef"}}
            return {"message": "Updating pull request branch."}

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

        adapter = GitHubCliAdapter()
        result = await adapter.resolve_conflicts("OmniNode-ai/omnimarket", 42)

        assert result == "deadbeef"
        assert calls == [
            ("GET", "/repos/OmniNode-ai/omnimarket/pulls/42", None),
            (
                "PUT",
                "/repos/OmniNode-ai/omnimarket/pulls/42/update-branch",
                {"expected_head_sha": "deadbeef"},
            ),
            ("GET", "/repos/OmniNode-ai/omnimarket/pulls/42", None),
        ]

    async def test_resolve_conflicts_raises_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BoomError(RuntimeError):
            pass

        def fake_rest_json(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> dict[str, object]:
            del path, body, token
            if method == "GET":
                return {"head": {"sha": "deadbeef"}}
            raise _BoomError("structural conflict - manual merge required")

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

        adapter = GitHubCliAdapter()
        with pytest.raises(RuntimeError, match="manual resolution"):
            await adapter.resolve_conflicts("OmniNode-ai/omnimarket", 42)

    async def test_cancel_obsolete_runs_protects_current_head_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F4 (OMN-13320): the current PR head run id must survive cleanup."""
        head_sha = "headsha000"
        cancel_calls: list[tuple[str, str]] = []

        def fake_rest_json(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> dict[str, object]:
            del body, token
            if path.endswith("/pulls/77"):
                return {"head": {"sha": head_sha, "ref": "feature/x"}}
            # listing workflow runs
            return {
                "workflow_runs": [
                    {"id": 9001, "head_sha": "oldsha111"},  # obsolete -> cancel
                    {"id": 9002, "head_sha": head_sha},  # current head -> protect
                    {"id": 9003, "head_sha": "oldsha222"},  # obsolete -> cancel
                ]
            }

        def fake_rest_no_content(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> None:
            del body, token
            cancel_calls.append((method, path))

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_no_content",
            fake_rest_no_content,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token",
            lambda: "fake-token",
        )

        adapter = GitHubCliAdapter()
        result = await adapter.cancel_obsolete_runs("OmniNode-ai/omnimarket", 77)

        # The current head run (9002) is never touched; both obsolete heads are
        # force-cancelled.
        assert cancel_calls == [
            (
                "POST",
                "/repos/OmniNode-ai/omnimarket/actions/runs/9001/force-cancel",
            ),
            (
                "POST",
                "/repos/OmniNode-ai/omnimarket/actions/runs/9003/force-cancel",
            ),
        ]
        cancelled_run_ids = {
            path.split("/runs/")[1].split("/")[0] for _, path in cancel_calls
        }
        assert "9002" not in cancelled_run_ids
        assert "force-cancelled 2 obsolete-head run(s)" in result
        assert "head run protected" in result

    async def test_cancel_obsolete_runs_skips_http_409_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 409 on force-cancel is stale metadata: skip, never retry/raise."""
        head_sha = "headsha000"
        cancel_calls: list[str] = []

        def fake_rest_json(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> dict[str, object]:
            del body, token
            if path.endswith("/pulls/88"):
                return {"head": {"sha": head_sha, "ref": "feature/y"}}
            return {
                "workflow_runs": [
                    {"id": 5001, "head_sha": "stalesha"},  # 409 -> skip
                    {"id": 5002, "head_sha": "oldsha"},  # cancels fine
                    {"id": 5003, "head_sha": head_sha},  # current head -> protect
                ]
            }

        def fake_rest_no_content(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> None:
            del body, token
            cancel_calls.append(path)
            if "/runs/5001/" in path:
                raise GitHubApiError(
                    "Cannot cancel a workflow re-run that has not yet queued",
                    status_code=409,
                )

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_no_content",
            fake_rest_no_content,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token",
            lambda: "fake-token",
        )

        adapter = GitHubCliAdapter()
        result = await adapter.cancel_obsolete_runs("OmniNode-ai/omnimarket", 88)

        # 409 is swallowed; the next obsolete run is still cancelled; head untouched.
        assert any("/runs/5001/force-cancel" in p for p in cancel_calls)
        assert any("/runs/5002/force-cancel" in p for p in cancel_calls)
        assert not any("/runs/5003/" in p for p in cancel_calls)
        assert "force-cancelled 1 obsolete-head run(s)" in result
        assert "skipped 1 stale" in result

    async def test_cancel_obsolete_runs_reraises_non_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-409 GitHub error during cancel must propagate (fail-loud)."""

        def fake_rest_json(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> dict[str, object]:
            del body, token
            if path.endswith("/pulls/99"):
                return {"head": {"sha": "headsha", "ref": "feature/z"}}
            return {"workflow_runs": [{"id": 6001, "head_sha": "oldsha"}]}

        def fake_rest_no_content(
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            token: str | None = None,
        ) -> None:
            del method, path, body, token
            raise GitHubApiError("server exploded", status_code=500)

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_no_content",
            fake_rest_no_content,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli._resolve_github_token",
            lambda: "fake-token",
        )

        adapter = GitHubCliAdapter()
        with pytest.raises(GitHubApiError, match="server exploded"):
            await adapter.cancel_obsolete_runs("OmniNode-ai/omnimarket", 99)

    def test_run_id_parser_handles_standard_urls(self) -> None:
        assert (
            _run_id_from_details_url(
                "https://github.com/OmniNode-ai/omnimarket/actions/runs/123456/job/1"
            )
            == "123456"
        )
        assert (
            _run_id_from_details_url(
                "https://github.com/x/y/actions/runs/123?check_suite_focus=true"
            )
            == "123"
        )
        assert _run_id_from_details_url("https://example.com/whatever") is None
        assert _run_id_from_details_url("") is None
