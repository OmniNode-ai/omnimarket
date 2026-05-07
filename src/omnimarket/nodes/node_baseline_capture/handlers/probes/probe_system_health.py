"""System health probe — checks each infrastructure service endpoint."""

from __future__ import annotations

import logging
import os
import time

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelServiceHealthSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_TIMEOUT_S = 3.0


def _services_from_env() -> list[tuple[str, str]]:
    """Build list of (name, url) tuples from environment variables."""
    candidates = [
        (
            "postgres",
            f"http://{os.environ.get('POSTGRES_HOST', '192.168.86.201')}:{os.environ.get('POSTGRES_PORT', '5436')}",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab Postgres; override via POSTGRES_HOST/PORT"
        ),
        (
            "redpanda",
            f"http://{os.environ.get('REDPANDA_ADMIN_HOST', '192.168.86.201')}:9644/v1/cluster/health",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab Redpanda admin; override via REDPANDA_ADMIN_HOST"
        ),
        (
            "valkey",
            "http://192.168.86.201:16379",  # onex-allow-internal-ip OMN-10580 reason="lab Valkey endpoint; no env-var override in this probe"
        ),
        (
            "qdrant",
            f"http://{os.environ.get('QDRANT_HOST', '192.168.86.201')}:{os.environ.get('QDRANT_PORT', '6333')}/healthz",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab Qdrant; override via QDRANT_HOST/PORT"
        ),
        (
            "llm_coder",
            f"{os.environ.get('LLM_CODER_URL', 'http://192.168.86.201:8000')}/health",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab LLM coder; override via LLM_CODER_URL"
        ),
        (
            "llm_coder_fast",
            f"{os.environ.get('LLM_CODER_FAST_URL', 'http://192.168.86.201:8001')}/health",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab LLM fast; override via LLM_CODER_FAST_URL"
        ),
        (
            "llm_embedding",
            f"{os.environ.get('LLM_EMBEDDING_URL', 'http://192.168.86.200:8100')}/health",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab embedding endpoint; override via LLM_EMBEDDING_URL"
        ),
    ]
    return candidates


class ProbeSystemHealth:
    """Probe that checks health of each infrastructure service."""

    name: str = "system_health"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Collect service health snapshots using httpx with 3s timeout.

        Returns list with healthy=False entries for unreachable services.
        """
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not available — skipping system_health probe")
            return []

        services = _services_from_env()
        results: list[ProbeSnapshotItem] = []

        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            for name, url in services:
                start = time.monotonic()
                try:
                    resp = await client.get(url)
                    latency_ms = (time.monotonic() - start) * 1000
                    healthy = resp.status_code < 500
                    results.append(
                        ModelServiceHealthSnapshot(
                            service=name,
                            healthy=healthy,
                            latency_ms=round(latency_ms, 1),
                            error=None if healthy else f"HTTP {resp.status_code}",
                        )
                    )
                except Exception as exc:
                    latency_ms = (time.monotonic() - start) * 1000
                    results.append(
                        ModelServiceHealthSnapshot(
                            service=name,
                            healthy=False,
                            latency_ms=round(latency_ms, 1),
                            error=str(exc)[:200],
                        )
                    )

        return results


__all__: list[str] = ["ProbeSystemHealth"]
