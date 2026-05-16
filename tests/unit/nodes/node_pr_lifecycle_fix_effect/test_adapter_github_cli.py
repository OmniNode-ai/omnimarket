# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest

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

        def fake_graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
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
            method: str, path: str, *, body: dict[str, object] | None = None
        ) -> None:
            del body
            rerun_calls.append((method, path))

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.graphql",
            fake_graphql,
        )
        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_no_content",
            fake_rest_no_content,
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
        def fake_graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
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

        adapter = GitHubCliAdapter()
        result = await adapter.rerun_failed_checks("OmniNode-ai/omnimarket", 42)

        assert "no failed checks" in result

    async def test_resolve_conflicts_calls_update_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_rest_json(
            method: str, path: str, *, body: dict[str, object] | None = None
        ) -> dict[str, object]:
            calls.append((method, path, body))
            if method == "GET":
                return {"head": {"sha": "deadbeef"}}
            return {"message": "Updating pull request branch."}

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )

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
            method: str, path: str, *, body: dict[str, object] | None = None
        ) -> dict[str, object]:
            del path, body
            if method == "GET":
                return {"head": {"sha": "deadbeef"}}
            raise _BoomError("structural conflict - manual merge required")

        monkeypatch.setattr(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli.rest_json",
            fake_rest_json,
        )

        adapter = GitHubCliAdapter()
        with pytest.raises(RuntimeError, match="manual resolution"):
            await adapter.resolve_conflicts("OmniNode-ai/omnimarket", 42)

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
