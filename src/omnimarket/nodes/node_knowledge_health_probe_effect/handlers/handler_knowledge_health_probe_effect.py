# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Effect handler that probes knowledge backends and returns raw probe results."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from omnimarket.enums.enum_knowledge_freshness_state import EnumKnowledgeFreshnessState
from omnimarket.events.knowledge_health import ModelKnowledgeBackendProbe
from omnimarket.nodes.node_knowledge_health_probe_effect.models.model_knowledge_health_probe_request import (
    ModelKnowledgeHealthProbeRequest,
)
from omnimarket.nodes.node_knowledge_health_probe_effect.models.model_knowledge_health_probe_result import (
    ModelKnowledgeHealthProbeResult,
)

logger = logging.getLogger(__name__)

_HttpGetFn = Callable[
    [str, float],
    Coroutine[Any, Any, tuple[int, dict[str, Any]]],
]


async def _default_http_get(
    url: str,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.status_code, resp.json()


class HandlerKnowledgeHealthProbeEffect:
    """Probes each configured knowledge backend and returns raw probe results.

    Inject ``http_get_fn`` in tests to avoid real network calls.
    """

    def __init__(self, http_get_fn: _HttpGetFn | None = None) -> None:
        self._http_get = http_get_fn or _default_http_get

    async def handle(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeHealthProbeResult:
        probes: list[ModelKnowledgeBackendProbe] = []
        for backend_id in request.backends:
            probe = await self._probe_backend(backend_id, request)
            probes.append(probe)
            logger.debug(
                "knowledge-health-probe backend=%s freshness=%s",
                backend_id,
                probe.freshness_state,
            )
        return ModelKnowledgeHealthProbeResult(backend_probes=tuple(probes))

    async def _probe_backend(
        self,
        backend_id: str,
        request: ModelKnowledgeHealthProbeRequest,
    ) -> ModelKnowledgeBackendProbe:
        try:
            if backend_id == "repowise":
                return await self._probe_repowise(request)
            if backend_id == "qdrant":
                return await self._probe_qdrant(request)
            if backend_id == "memgraph":
                return await self._probe_memgraph(request)
            if backend_id == "kb_repo":
                return await self._probe_kb_repo(request)
            if backend_id == "agent_learning":
                return await self._probe_agent_learning(request)
        except Exception as exc:
            logger.warning(
                "knowledge-health-probe backend=%s error: %s", backend_id, exc
            )
            return ModelKnowledgeBackendProbe(
                backend_id=backend_id,
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )
        return ModelKnowledgeBackendProbe(
            backend_id=backend_id,
            freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
            error=f"Unknown backend_id: {backend_id!r}",
        )

    async def _probe_repowise(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeBackendProbe:
        base_url = (request.repowise_url or os.environ.get("REPOWISE_URL", "")).rstrip(
            "/"
        )
        if not base_url:
            return ModelKnowledgeBackendProbe(
                backend_id="repowise",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="REPOWISE_URL not configured",
            )
        try:
            _status, data = await self._http_get(
                f"{base_url}/api/health", request.timeout_seconds
            )
            index_age_days: int | None = data.get("index_age_days")
            freshness = _freshness_from_age_days(index_age_days)
            return ModelKnowledgeBackendProbe(
                backend_id="repowise",
                freshness_state=freshness,
                entry_count=data.get("indexed_file_count", 0),
                last_updated_seconds_ago=(
                    int(index_age_days * 86400) if index_age_days is not None else None
                ),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ModelKnowledgeBackendProbe(
                backend_id="repowise",
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _probe_qdrant(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeBackendProbe:
        base_url = (request.qdrant_url or os.environ.get("QDRANT_URL", "")).rstrip("/")
        if not base_url:
            return ModelKnowledgeBackendProbe(
                backend_id="qdrant",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="QDRANT_URL not configured",
            )
        try:
            _status, data = await self._http_get(
                f"{base_url}/collections", request.timeout_seconds
            )
            collections: list[dict[str, Any]] = (data.get("result") or {}).get(
                "collections"
            ) or []
            total_points = sum(int(c.get("vectors_count") or 0) for c in collections)
            freshness = (
                EnumKnowledgeFreshnessState.FRESH
                if total_points > 0
                else EnumKnowledgeFreshnessState.STALE
            )
            return ModelKnowledgeBackendProbe(
                backend_id="qdrant",
                freshness_state=freshness,
                entry_count=total_points,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ModelKnowledgeBackendProbe(
                backend_id="qdrant",
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _probe_memgraph(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeBackendProbe:
        # Memgraph is probed via its HTTP status endpoint if available;
        # bolt-only deployments return unknown when the HTTP health path is absent.
        bolt_url = request.memgraph_bolt_url or os.environ.get("MEMGRAPH_BOLT_URL", "")
        if not bolt_url:
            return ModelKnowledgeBackendProbe(
                backend_id="memgraph",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="MEMGRAPH_BOLT_URL not configured",
            )
        # Derive HTTP health endpoint from bolt URL heuristic (bolt 7687 → http 7444)
        http_health_url = os.environ.get("MEMGRAPH_HTTP_URL", "")
        if not http_health_url:
            return ModelKnowledgeBackendProbe(
                backend_id="memgraph",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="MEMGRAPH_HTTP_URL not configured; cannot probe node/edge counts",
            )
        try:
            _status, data = await self._http_get(
                f"{http_health_url.rstrip('/')}/health", request.timeout_seconds
            )
            node_count: int = data.get("node_count", 0)
            edge_count: int = data.get("edge_count", 0)
            total = node_count + edge_count
            freshness = (
                EnumKnowledgeFreshnessState.FRESH
                if total > 0
                else EnumKnowledgeFreshnessState.STALE
            )
            return ModelKnowledgeBackendProbe(
                backend_id="memgraph",
                freshness_state=freshness,
                entry_count=total,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ModelKnowledgeBackendProbe(
                backend_id="memgraph",
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _probe_kb_repo(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeBackendProbe:
        import subprocess
        import time

        kb_path = request.kb_repo_path or os.environ.get("KB_REPO_PATH", "")
        if not kb_path:
            return ModelKnowledgeBackendProbe(
                backend_id="kb_repo",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="KB_REPO_PATH not configured",
            )
        try:
            result = subprocess.run(
                ["git", "-C", kb_path, "log", "-1", "--format=%ct"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return ModelKnowledgeBackendProbe(
                    backend_id="kb_repo",
                    freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                    error=f"git log failed: {result.stderr.strip()}",
                )
            last_commit_ts = int(result.stdout.strip())
            seconds_ago = int(time.time()) - last_commit_ts
            freshness = _freshness_from_age_days(seconds_ago / 86400)
            return ModelKnowledgeBackendProbe(
                backend_id="kb_repo",
                freshness_state=freshness,
                last_updated_seconds_ago=seconds_ago,
            )
        except Exception as exc:
            return ModelKnowledgeBackendProbe(
                backend_id="kb_repo",
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _probe_agent_learning(
        self, request: ModelKnowledgeHealthProbeRequest
    ) -> ModelKnowledgeBackendProbe:
        # Agent learning retrieval metrics are surfaced via the omnimemory health endpoint.
        retrieval_url = os.environ.get("OMNIMEMORY_HEALTH_URL", "")
        if not retrieval_url:
            return ModelKnowledgeBackendProbe(
                backend_id="agent_learning",
                freshness_state=EnumKnowledgeFreshnessState.UNKNOWN,
                error="OMNIMEMORY_HEALTH_URL not configured",
            )
        try:
            _status, data = await self._http_get(
                retrieval_url.rstrip("/"), request.timeout_seconds
            )
            doc_count: int = data.get("document_count", 0)
            freshness = (
                EnumKnowledgeFreshnessState.FRESH
                if doc_count > 0
                else EnumKnowledgeFreshnessState.STALE
            )
            return ModelKnowledgeBackendProbe(
                backend_id="agent_learning",
                freshness_state=freshness,
                entry_count=doc_count,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ModelKnowledgeBackendProbe(
                backend_id="agent_learning",
                freshness_state=EnumKnowledgeFreshnessState.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
            )


def _freshness_from_age_days(age_days: float | None) -> EnumKnowledgeFreshnessState:
    if age_days is None:
        return EnumKnowledgeFreshnessState.UNKNOWN
    if age_days <= 1:
        return EnumKnowledgeFreshnessState.FRESH
    if age_days <= 7:
        return EnumKnowledgeFreshnessState.STALE
    return EnumKnowledgeFreshnessState.DEGRADED


__all__ = ["HandlerKnowledgeHealthProbeEffect"]
