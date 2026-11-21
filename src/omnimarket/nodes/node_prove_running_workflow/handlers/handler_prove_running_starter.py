# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The starter: one workflow start per NOT RUNNING line of the integration pass, oldest merge first, with no cap.

The lines arrive as the integration pass's own typed batch (``ModelIntegrationReadingBatch``, the shape the pass hands
the floor publish effect), so nothing here parses the integration ledger's text. The merge sha of each line comes from
an injected resolver (``onex scm pr-merge-commit`` in the CLI) and each start goes to an injected starter, so the
handler is tested without a server. Every line read is either a change to start or an unresolved line that the batch
answers with its own result row; nothing is dropped, so ``lines_read == len(changes) + len(unresolved)``.
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
    ModelProveRunningUnresolvedLine,
)


class ProtocolMergeShaResolver(Protocol):
    """Answers the merge sha of ``repository#number``, or None when it cannot be read."""

    def merge_sha(self, repository: str, number: int) -> str | None: ...


def _target(text: str) -> str:
    return re.sub(r"\s+", "-", text.strip())


class HandlerProveRunningStarter:
    """Select every NOT RUNNING line (oldest merge first) and resolve each to a change to prove.

    ``workers`` bounds how many merge shas are read at once: one read takes seconds, and a projection holds hundreds
    of lines, so a sequential read would outlast the step's timeout. The result order never depends on it.
    """

    def __init__(
        self, resolver: ProtocolMergeShaResolver | None = None, *, workers: int = 1
    ) -> None:
        # Optional so the routed handler boots without a resolver; a start without one fails loudly in handle().
        self._resolver = resolver
        self._workers = max(1, workers)

    def _resolve(self, lines: list[ModelIntegrationReading]) -> list[str | None]:
        resolver = self._resolver
        assert resolver is not None
        if self._workers == 1 or len(lines) < 2:
            return [resolver.merge_sha(r.repository, r.number) for r in lines]
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(
                pool.map(lambda r: resolver.merge_sha(r.repository, r.number), lines)
            )

    def handle(
        self, request: ModelProveRunningStartRequest
    ) -> ModelProveRunningStartBatch:
        if self._resolver is None:
            raise RuntimeError(
                "HandlerProveRunningStarter needs a merge sha resolver to start a batch"
            )
        unique: dict[str, ModelIntegrationReading] = {}
        for reading in sorted(
            (
                r
                for r in request.batch.readings
                if r.state is EnumIntegrationState.NOT_RUNNING
            ),
            key=lambda r: (r.merged_at, r.key),
        ):
            unique.setdefault(reading.key, reading)
        lines = list(unique.values())
        changes: list[ModelProveRunningChange] = []
        skipped: list[str] = []
        unresolved: list[ModelProveRunningUnresolvedLine] = []
        for line, sha in zip(lines, self._resolve(lines), strict=True):
            if not sha:
                skipped.append(f"{line.key}: merge sha unread")
                unresolved.append(
                    ModelProveRunningUnresolvedLine(
                        repository=line.repository,
                        number=line.number,
                        target=_target(line.target),
                        owner=line.owner,
                        next_step=line.step,
                        reason="merge sha unread",
                    )
                )
                continue
            changes.append(
                ModelProveRunningChange(
                    repository=line.repository,
                    number=line.number,
                    merge_sha=sha,
                    target=_target(line.target),
                    owner=line.owner,
                    next_step=line.step,
                )
            )
        return ModelProveRunningStartBatch(
            changes=tuple(changes),
            skipped=tuple(skipped),
            unresolved=tuple(unresolved),
            lines_read=len(lines),
        )
