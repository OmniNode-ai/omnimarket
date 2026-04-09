"""ProbeSystemHealth — check health of infrastructure services via HTTP."""

from __future__ import annotations

import logging
import os
import time

import httpx

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelServiceHealthSnapshot,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 3.0

# Service definitions: (name, env_var_for_url, fallback_url, health_path)
_SERVICE_DEFINITIONS: list[tuple[str, str, str, str]] = [
    (
        "postgres",
        "",
        "",
        "",  # TCP-only — checked via env var presence (health check via pg ping not HTTP)
    ),
    (
        "llm_coder",
        "LLM_CODER_URL",
        "http://192.168.86.201:8000",
        "/health",
    ),
    (
        "llm_coder_fast",
        "LLM_CODER_FAST_URL",
        "http://192.168.86.201:8001",
        "/health",
    ),
    (
        "llm_embedding",
        "LLM_EMBEDDING_URL",
        "http://192.168.86.200:8100",
        "/health",
    ),
    (
        "llm_deepseek_r1",
        "LLM_DEEPSEEK_R1_URL",
        "http://192.168.86.200:8101",
        "/health",
    ),
    (
        "qdrant",
        "QDRANT_URL",
        "http://192.168.86.201:6333",
        "/healthz",
    ),
]


def _check_http(name: str, url: str) -> ModelServiceHealthSnapshot:
    """Perform a single HTTP health check and return a snapshot."""
    start = time.monotonic()
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(url)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        healthy = resp.status_code < 500
        error: str | None = None if healthy else f"HTTP {resp.status_code}"
        return ModelServiceHealthSnapshot(
            service=name,
            healthy=healthy,
            latency_ms=latency_ms,
            error=error,
        )
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return ModelServiceHealthSnapshot(
            service=name,
            healthy=False,
            latency_ms=latency_ms,
            error=str(exc),
        )


class ProbeSystemHealth:
    """Check health of all platform services."""

    name: str = "system_health"

    async def collect(self) -> list[ModelServiceHealthSnapshot]:
        """Return service health snapshots; never raises."""
        snapshots: list[ModelServiceHealthSnapshot] = []

        for service_name, env_var, fallback_url, health_path in _SERVICE_DEFINITIONS:
            # Skip postgres — HTTP health check not applicable
            if service_name == "postgres":
                continue

            base_url = (
                os.environ.get(env_var, fallback_url) if env_var else fallback_url
            )
            if not base_url:
                continue

            url = base_url.rstrip("/") + health_path
            try:
                snapshot = _check_http(service_name, url)
            except Exception as exc:
                logger.warning(
                    "probe_system_health: unexpected error for %s: %s",
                    service_name,
                    exc,
                )
                snapshot = ModelServiceHealthSnapshot(
                    service=service_name,
                    healthy=False,
                    latency_ms=None,
                    error=str(exc),
                )
            snapshots.append(snapshot)

        return snapshots


__all__: list[str] = ["ProbeSystemHealth"]
