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
    """Build list of (name, url) tuples from environment variables.

    Services with unconfigured URLs are omitted — callers must set the
    appropriate env vars to include them in health probes.
    """
    candidates: list[tuple[str, str]] = []

    postgres_host = os.environ.get("POSTGRES_HOST", "")  # contract-config-ok: config  # fmt: skip
    postgres_port = os.environ.get("POSTGRES_PORT", "")  # contract-config-ok: config  # fmt: skip
    if postgres_host and postgres_port:
        candidates.append(("postgres", f"http://{postgres_host}:{postgres_port}"))

    redpanda_host = os.environ.get("REDPANDA_ADMIN_HOST", "")  # contract-config-ok: config  # fmt: skip
    redpanda_port = os.environ.get("REDPANDA_ADMIN_PORT", "9644")  # contract-config-ok: config  # fmt: skip
    if redpanda_host:
        candidates.append(
            ("redpanda", f"http://{redpanda_host}:{redpanda_port}/v1/cluster/health")
        )

    valkey_host = os.environ.get("VALKEY_HOST", "")  # contract-config-ok: config  # fmt: skip
    valkey_port = os.environ.get("VALKEY_PORT", "")  # contract-config-ok: config  # fmt: skip
    if valkey_host and valkey_port:
        candidates.append(("valkey", f"http://{valkey_host}:{valkey_port}"))

    qdrant_host = os.environ.get("QDRANT_HOST", "")  # contract-config-ok: config  # fmt: skip
    qdrant_port = os.environ.get("QDRANT_PORT", "")  # contract-config-ok: config  # fmt: skip
    if qdrant_host and qdrant_port:
        candidates.append(("qdrant", f"http://{qdrant_host}:{qdrant_port}/healthz"))

    llm_coder_url = os.environ.get("LLM_CODER_URL", "")  # contract-config-ok: config  # fmt: skip
    if llm_coder_url:
        candidates.append(("llm_coder", f"{llm_coder_url}/health"))

    llm_coder_fast_url = os.environ.get("LLM_CODER_FAST_URL", "")  # contract-config-ok: config  # fmt: skip
    if llm_coder_fast_url:
        candidates.append(("llm_coder_fast", f"{llm_coder_fast_url}/health"))

    llm_embedding_url = os.environ.get("LLM_EMBEDDING_URL", "")  # contract-config-ok: config  # fmt: skip
    if llm_embedding_url:
        candidates.append(("llm_embedding", f"{llm_embedding_url}/health"))

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
