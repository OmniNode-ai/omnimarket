# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for GitHubCliAdapter (OMN-9284).

Replaces ``asyncio.create_subprocess_exec`` with a recorder so we can assert
on the exact ``gh`` argv invoked for each block-reason path without spawning
any real processes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
    GitHubCliAdapter,
    _run_id_from_details_url,
)


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = rc

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.fixture
def subprocess_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[_FakeProc]], list[list[str]]]:
    """Return a helper that installs a fake ``create_subprocess_exec``.

    Each call pops one _FakeProc from the supplied queue; the argv is
    appended to a shared list the test can assert on.
    """

    def install(queue: list[_FakeProc]) -> list[list[str]]:
        calls: list[list[str]] = []

        async def fake_exec(*argv: str, **_kwargs: object) -> _FakeProc:
            calls.append(list(argv))
            if not queue:
                return _FakeProc()
            return queue.pop(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return calls

    return install


@pytest.mark.unit
class TestGitHubCliAdapter:
    async def test_rerun_failed_checks_enumerates_and_reruns_each(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        """rerun_failed_checks: one `gh pr view` + one `gh run rerun` per failed run."""
        pr_view_payload = {
            "statusCheckRollup": [
                {
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/111/job/1",
                },
                {
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/222/job/2",
                },
                {
                    "conclusion": "TIMED_OUT",
                    "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/333/job/3",
                },
                {
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/OmniNode-ai/omnimarket/actions/runs/111/job/4",
                },  # dedup
            ]
        }
        calls = subprocess_recorder(
            [
                _FakeProc(stdout=json.dumps(pr_view_payload).encode()),
                _FakeProc(),
                _FakeProc(),
            ]
        )

        adapter = GitHubCliAdapter()
        result = await adapter.rerun_failed_checks("OmniNode-ai/omnimarket", 42)

        assert "rerequested 2 failed run(s)" in result
        assert calls[0][:4] == ["gh", "pr", "view", "42"]
        assert calls[1] == [
            "gh",
            "run",
            "rerun",
            "111",
            "--failed",
            "--repo",
            "OmniNode-ai/omnimarket",
        ]
        assert calls[2] == [
            "gh",
            "run",
            "rerun",
            "333",
            "--failed",
            "--repo",
            "OmniNode-ai/omnimarket",
        ]

    async def test_rerun_failed_checks_no_failed_runs(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        pr_view_payload = {
            "statusCheckRollup": [
                {
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://github.com/x/y/actions/runs/1/job/1",
                }
            ]
        }
        calls = subprocess_recorder(
            [_FakeProc(stdout=json.dumps(pr_view_payload).encode())]
        )

        adapter = GitHubCliAdapter()
        result = await adapter.rerun_failed_checks("OmniNode-ai/omnimarket", 42)

        assert "no failed checks" in result
        assert len(calls) == 1  # only pr view, no rerun

    async def test_resolve_conflicts_calls_update_branch(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        calls = subprocess_recorder([_FakeProc(rc=0)])
        adapter = GitHubCliAdapter()
        result = await adapter.resolve_conflicts("OmniNode-ai/omnimarket", 42)

        assert "update-branch succeeded" in result
        assert calls[0] == [
            "gh",
            "pr",
            "update-branch",
            "42",
            "--repo",
            "OmniNode-ai/omnimarket",
        ]

    async def test_resolve_conflicts_raises_on_failure(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        subprocess_recorder(
            [_FakeProc(rc=1, stderr=b"structural conflict - manual merge required")]
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


def _unused(x: Awaitable[object]) -> None:  # keep import useful for type checker
    del x
