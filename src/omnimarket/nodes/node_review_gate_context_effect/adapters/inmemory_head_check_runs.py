# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory head check runs: the default backend; keeps runs by id, with failure injection flags."""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    ModelHeadCheckRun,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ReviewContextPortError,
)


class InMemoryHeadCheckRuns:
    """Implements both the reader and poster ports over a list of ModelHeadCheckRun."""

    def __init__(
        self,
        fixture: Iterable[ModelHeadCheckRun] = (),
        *,
        fail_reads: bool = False,
        fail_posts: bool = False,
        drop_posts: bool = False,
    ) -> None:
        self._runs: list[ModelHeadCheckRun] = list(fixture)
        self._next_id = max((r.check_run_id for r in self._runs), default=0) + 1
        self._calls = 0
        self._posted: list[dict[str, object]] = []
        self._fail_reads = fail_reads
        self._fail_posts = fail_posts
        self._drop_posts = drop_posts

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def posted(self) -> list[dict[str, object]]:
        return self._posted

    def read(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelHeadCheckRun, ...]:
        self._calls += 1
        if self._fail_reads:
            raise ReviewContextPortError("injected read failure")
        return tuple(
            r for r in self._runs if r.head_sha == head_sha and r.name == check_name
        )

    def post(
        self,
        *,
        correlation_id: str,
        repository: str,
        head_sha: str,
        check_name: str,
        conclusion: str,
        title: str,
        summary: str,
    ) -> int:
        self._calls += 1
        if self._fail_posts:
            raise ReviewContextPortError("injected post failure")
        run_id = self._next_id
        self._next_id += 1
        self._posted.append(
            {
                "correlation_id": correlation_id,
                "repository": repository,
                "head_sha": head_sha,
                "check_name": check_name,
                "conclusion": conclusion,
                "title": title,
                "summary": summary,
            }
        )
        if not self._drop_posts:
            self._runs.append(
                ModelHeadCheckRun(
                    check_run_id=run_id,
                    name=check_name,
                    head_sha=head_sha,
                    status="completed",
                    conclusion=conclusion,
                )
            )
        return run_id
