# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The starter: one workflow start per NOT RUNNING line of the integration pass, oldest merge first.

The lines arrive as the integration pass's own typed batch (``ModelIntegrationReadingBatch``, the shape the pass hands
the floor publish effect), so nothing here parses the integration ledger's text. The merge sha of each line comes from
an injected resolver (``onex scm pr-merge-commit`` in the CLI) and each start goes to an injected starter, so the
handler is tested without a server.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from omnimarket.models.integration_reading import (
    EnumIntegrationState,
    ModelIntegrationReading,
)
from omnimarket.nodes.node_prove_running_workflow.models.model_prove_running import (
    ModelProveRunningChange,
    ModelProveRunningStartBatch,
    ModelProveRunningStartRequest,
)


class ProtocolMergeShaResolver(Protocol):
    """Answers the merge sha of ``repository#number``, or None when it cannot be read."""

    def merge_sha(self, repository: str, number: int) -> str | None: ...


def _target(text: str) -> str:
    return re.sub(r"\s+", "-", text.strip())


class HandlerProveRunningStarter:
    """Select every NOT RUNNING line (oldest merge first) and resolve each to a change to prove.

    A caller may name a limit to start fewer. The merge shas are resolved by ``workers`` concurrent readers, a declared
    size that bounds the readers and never the lines: each line is one change, in order.
    """

    def __init__(
        self, resolver: ProtocolMergeShaResolver | None = None, workers: int = 1
    ) -> None:
        # Optional so the routed handler boots without a resolver; a start without one fails loudly in handle().
        self._resolver = resolver
        self._workers = max(1, workers)

    def handle(
        self, request: ModelProveRunningStartRequest
    ) -> ModelProveRunningStartBatch:
        if self._resolver is None:
            raise RuntimeError(
                "HandlerProveRunningStarter needs a merge sha resolver to start a batch"
            )
        lines = sorted(
            (
                r
                for r in request.batch.readings
                if r.state is EnumIntegrationState.NOT_RUNNING
            ),
            key=lambda r: (r.merged_at, r.key),
        )
        unique: list[ModelIntegrationReading] = []
        seen: set[str] = set()
        for line in lines:
            if line.key not in seen:
                seen.add(line.key)
                unique.append(line)
        resolver = self._resolver
        answered: list[tuple[ModelIntegrationReading, str | None]] = []
        if request.limit is None:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                shas = list(
                    pool.map(
                        lambda r: resolver.merge_sha(r.repository, r.number), unique
                    )
                )
            answered = list(zip(unique, shas, strict=True))
        else:
            found = 0
            for line in unique:
                if found >= request.limit:
                    break
                sha = resolver.merge_sha(line.repository, line.number)
                found += 1 if sha else 0
                answered.append((line, sha))
        changes: list[ModelProveRunningChange] = []
        skipped: list[str] = []
        for line, sha in answered:
            if not sha:
                skipped.append(f"{line.key}: merge sha unread")
                continue
            changes.append(
                ModelProveRunningChange(
                    repository=line.repository,
                    number=line.number,
                    merge_sha=sha,
                    target=_target(line.target),
                )
            )
        return ModelProveRunningStartBatch(
            changes=tuple(changes), skipped=tuple(skipped)
        )
