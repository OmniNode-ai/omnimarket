# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabFocusedTestRunEffect -- the focused test run, hosted on the lab
docker host and reached over the bus (OMN-19458).

Canonical def-B handler: ``handle(request: ModelFocusedTestRunRequest) ->
ModelFocusedTestRunReceipt``. No ``Plugin*`` base, no envelope type.

EXTEND-vs-NET-NEW. The sequence is node_push_validation_effect's
``run_focused_test_run`` (``HandlerFocusedTestRunEffect``), reused whole; this
handler only binds it to ``LocalShellFocusedRunSubprocess``, because this node
runs on the host that owns the docker daemon. It is a separate node, not a
fourth operation on the push-validation contract, because that contract's
``runtime_profiles: [effects]`` plus ``event_bus`` block makes every shared
effects runtime attach it, and those runtimes have no docker: an attached copy
would answer each command with ``infra_error`` and race the lab host.

REPOSITORY OWNERS. A focused run builds an environment image from the
repository's own lock file, which is the one step that reaches the network.
Over a bus, anyone who can publish to the command topic can name a repository,
so the handler refuses any owner outside ``allowed_owners`` before anything is
fetched, with an ``infra_error`` receipt that says so.
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_focused_test_run_effect.protocols.local_shell_focused_run_subprocess import (
    LocalShellFocusedRunSubprocess,
)
from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    HandlerFocusedTestRunEffect,
    ModelFocusedTestRunReceipt,
    ModelFocusedTestRunRequest,
    ProtocolFocusedTestRunClient,
)

logger = logging.getLogger(__name__)

#: The repository owners a bus-delivered run may name. The first slice runs
#: the platform's own public repositories only.
DEFAULT_ALLOWED_OWNERS: frozenset[str] = frozenset({"OmniNode-ai"})


class HandlerLabFocusedTestRunEffect:
    """EFFECT handler: one focused test run on the host this process runs on."""

    def __init__(
        self,
        client: ProtocolFocusedTestRunClient | None = None,
        allowed_owners: frozenset[str] = DEFAULT_ALLOWED_OWNERS,
    ) -> None:
        self._inner = HandlerFocusedTestRunEffect(
            client or LocalShellFocusedRunSubprocess()
        )
        self._allowed_owners = allowed_owners

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return await self._inner.handle(request)

    def run_sync(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt:
        refusal = self._refusal(request)
        if refusal is not None:
            return refusal
        return self._inner.run_sync(request)

    def _refusal(
        self, request: ModelFocusedTestRunRequest
    ) -> ModelFocusedTestRunReceipt | None:
        owner = request.repo.split("/", 1)[0]
        if owner in self._allowed_owners:
            return None
        logger.warning(
            "focused_test_run refused: owner=%s correlation_id=%s",
            owner,
            request.correlation_id,
        )
        return ModelFocusedTestRunReceipt(
            correlation_id=request.correlation_id,
            ref_role=request.ref_role,
            attempt=request.attempt,
            commit_sha=request.commit_sha,
            test_node_id=request.test_node_id,
            status=EnumFocusedTestRunStatus.INFRA_ERROR,
            detail=(
                f"repository owner {owner!r} is not one this lab host runs "
                f"(allowed: {sorted(self._allowed_owners)}); nothing was fetched"
            ),
        )


__all__ = ["DEFAULT_ALLOWED_OWNERS", "HandlerLabFocusedTestRunEffect"]
