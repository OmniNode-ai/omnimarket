# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory head statuses: the default backend; keeps statuses by id, with failure injection flags."""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.models.model_review_context_post import (
    ModelHeadStatus,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ReviewContextPortError,
)


class InMemoryHeadStatuses:
    """Implements both the reader and poster ports over a list of ModelHeadStatus."""

    def __init__(
        self,
        fixture: Iterable[ModelHeadStatus] = (),
        *,
        fail_reads: bool = False,
        fail_posts: bool = False,
        drop_posts: bool = False,
    ) -> None:
        self._statuses: list[ModelHeadStatus] = list(fixture)
        self._next_id = max((s.status_id for s in self._statuses), default=0) + 1
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
        self, *, repository: str, head_sha: str, context: str
    ) -> tuple[ModelHeadStatus, ...]:
        self._calls += 1
        if self._fail_reads:
            raise ReviewContextPortError("injected read failure")
        return tuple(
            s for s in self._statuses if s.head_sha == head_sha and s.context == context
        )

    def post(
        self,
        *,
        correlation_id: str,
        repository: str,
        head_sha: str,
        context: str,
        state: str,
        target_url: str,
        description: str,
    ) -> int:
        self._calls += 1
        if self._fail_posts:
            raise ReviewContextPortError("injected post failure")
        status_id = self._next_id
        self._next_id += 1
        self._posted.append(
            {
                "correlation_id": correlation_id,
                "repository": repository,
                "head_sha": head_sha,
                "context": context,
                "state": state,
                "target_url": target_url,
                "description": description,
            }
        )
        if not self._drop_posts:
            self._statuses.append(
                ModelHeadStatus(
                    status_id=status_id,
                    context=context,
                    head_sha=head_sha,
                    state=state,
                )
            )
        return status_id
