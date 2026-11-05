# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect handler for node_github_repo_gateway_effect.

Resolves the GitHub token at the effect boundary, builds the real read
transport, and dispatches the requested read operation. Read-only: it performs
network reads and emits the typed result as its terminal event — no mutation of
GitHub state.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelGithubGatewayResponse,
)
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.token_resolver import (
    resolve_github_token_async,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    RealGitHubReadTransport,
)

from .handler_job_log_cache import HandlerJobLogCache, cache_policy

_HANDLER_ID = "node_github_repo_gateway_effect"


class HandlerGithubRepoGatewayEffect:
    """EFFECT: run one read operation against the GitHub API, typed result out."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    async def handle(
        self, request: ModelGithubGatewayRequest
    ) -> ModelHandlerOutput[None]:
        """Resolve the token, dispatch the read, emit the typed result event."""
        if (
            request.cache_only
            and request.operation is not EnumGithubGatewayOperation.JOB_LOG_TAIL
        ):
            raise ValueError("cache_only is supported only for job_log_tail")
        transport = (
            None
            if request.cache_only
            else RealGitHubReadTransport(await resolve_github_token_async())
        )
        result: GithubGatewayResult
        if request.operation is EnumGithubGatewayOperation.JOB_LOG_TAIL:
            policy = cache_policy()
            assert policy.root is not None
            result = await asyncio.to_thread(
                HandlerJobLogCache(
                    policy.root, transport, ttl_seconds=policy.ttl_seconds
                ).handle,
                request,
            )
        else:
            assert transport is not None
            result = await asyncio.to_thread(dispatch, request, transport)

        response = ModelGithubGatewayResponse(
            correlation_id=request.correlation_id,
            result=result,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=(response,),
        )


__all__: list[str] = ["HandlerGithubRepoGatewayEffect"]
