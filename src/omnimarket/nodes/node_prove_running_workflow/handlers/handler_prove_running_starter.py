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
from typing import Protocol

from omnimarket.models.integration_reading import EnumIntegrationState
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
    """Select the NOT RUNNING lines (oldest merge first, up to the limit) and resolve each to a change to prove."""

    def __init__(self, resolver: ProtocolMergeShaResolver) -> None:
        self._resolver = resolver

    def handle(
        self, request: ModelProveRunningStartRequest
    ) -> ModelProveRunningStartBatch:
        lines = sorted(
            (
                r
                for r in request.batch.readings
                if r.state is EnumIntegrationState.NOT_RUNNING
            ),
            key=lambda r: (r.merged_at, r.key),
        )
        changes: list[ModelProveRunningChange] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if len(changes) >= request.limit:
                break
            if line.key in seen:
                continue
            seen.add(line.key)
            sha = self._resolver.merge_sha(line.repository, line.number)
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
