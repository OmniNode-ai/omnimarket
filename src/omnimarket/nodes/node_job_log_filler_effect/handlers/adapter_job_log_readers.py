# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two readers of the filler's profiles (OMN-23032).

``InMemoryJobLogReader`` (profile ``inmemory``) fills this host's job-log cache through the
gateway's own ``JobLogCacheTransport``, with the token the gateway resolves at its boundary.
``BusJobLogReader`` (profile ``redpanda``) publishes the gateway's typed request on the
bus, so the deployed gateway fills its own, shared cache.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

from omnimarket.handlers.scm.handler_gateway_read_client import (
    GATEWAY_BUS_LANE_ENV,
    read_over_bus,
)
from omnimarket.models.github_landing.model_transport_error import GitHubTransportError
from omnimarket.models.model_gateway_node_skew import GatewayReadError
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelJobLogTailResult,
)
from omnimarket.models.model_job_log_cache_miss import JOB_LOG_CACHE_MISS
from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
    JobLogCacheTransport,
)
from omnimarket.nodes.node_github_repo_gateway_effect.token_resolver import (
    resolve_github_token,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    GitHubReadTransportProtocol,
    RealGitHubReadTransport,
)
from omnimarket.nodes.node_job_log_filler_effect.protocols.protocol_job_log_reader import (
    JobLogCacheMissError,
    JobLogNotFoundError,
)

_LOG_NOT_FOUND = re.compile(r"job log not cached: HTTP 404\b")


def is_log_not_found(message: str) -> bool:
    """True when a failure text says the gateway answered the job's log is not served: HTTP 404."""
    return _LOG_NOT_FOUND.search(message) is not None


def _request(
    repo: str, job_id: int, *, cache_only: bool, tail_bytes: int
) -> ModelGithubGatewayRequest:
    return ModelGithubGatewayRequest(
        operation=EnumGithubGatewayOperation.JOB_LOG_TAIL,
        repo=repo,
        job_id=job_id,
        cache_only=cache_only,
        tail_bytes=tail_bytes,
    )


class InMemoryJobLogReader:
    """Fill this host's cache in process; the transport is built only for a live read."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int,
        tail_bytes: int,
        transport_factory: Callable[[], GitHubReadTransportProtocol] | None = None,
    ) -> None:
        self.cache_path: str | None = str(root)
        self._root = root
        self._ttl = ttl_seconds
        self._tail = tail_bytes
        self._factory = transport_factory or self._real_transport

    @staticmethod
    def _real_transport() -> GitHubReadTransportProtocol:
        return cast(
            GitHubReadTransportProtocol, RealGitHubReadTransport(resolve_github_token())
        )

    def read(
        self,
        repo: str,
        job_id: int,
        *,
        cache_only: bool,
        tail_bytes: int | None = None,
    ) -> ModelJobLogTailResult:
        transport = None if cache_only else self._factory()
        cache = JobLogCacheTransport(self._root, transport, ttl_seconds=self._ttl)
        try:
            result = cache.handle(
                _request(
                    repo,
                    job_id,
                    cache_only=cache_only,
                    tail_bytes=tail_bytes or self._tail,
                )
            )
        except GitHubTransportError as exc:
            if str(exc).startswith(JOB_LOG_CACHE_MISS):
                raise JobLogCacheMissError(str(exc)) from exc
            if is_log_not_found(str(exc)):
                raise JobLogNotFoundError(str(exc)) from exc
            raise
        if not isinstance(result, ModelJobLogTailResult):
            raise GatewayReadError(f"gateway answered {type(result).__name__}")
        return result


class BusJobLogReader:
    """Ask the deployed gateway over the lane's bus; it fills its own cache."""

    def __init__(self, *, lane: str, timeout_seconds: float, tail_bytes: int) -> None:
        self.cache_path: str | None = None
        self._lane = lane
        self._timeout = timeout_seconds
        self._tail = tail_bytes

    def read(
        self,
        repo: str,
        job_id: int,
        *,
        cache_only: bool,
        tail_bytes: int | None = None,
    ) -> ModelJobLogTailResult:
        # read_over_bus takes the lane from this variable, which the profile names.
        os.environ[GATEWAY_BUS_LANE_ENV] = self._lane
        try:
            result = read_over_bus(
                _request(
                    repo,
                    job_id,
                    cache_only=cache_only,
                    tail_bytes=tail_bytes or self._tail,
                ),
                timeout_s=self._timeout,
            )
        except GatewayReadError as exc:
            if JOB_LOG_CACHE_MISS in str(exc):
                raise JobLogCacheMissError(str(exc)) from exc
            if is_log_not_found(str(exc)):
                raise JobLogNotFoundError(str(exc)) from exc
            raise
        if not isinstance(result, ModelJobLogTailResult):
            raise GatewayReadError(f"gateway answered {type(result).__name__}")
        return result


__all__ = ["BusJobLogReader", "InMemoryJobLogReader", "is_log_not_found"]
