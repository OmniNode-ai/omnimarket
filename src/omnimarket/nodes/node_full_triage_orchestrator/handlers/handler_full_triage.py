# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeFullTriageOrchestrator — unified read-only diagnosis aggregator.

Takes a list of probes, runs them in parallel with per-probe timeout + graceful
error capture, aggregates findings into a single deterministically-ranked
ModelTriageReport. Pure read-only — never mutates state.

Sub-probes are wired in OMN-9324..9327. This handler is the shared skeleton.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from time import perf_counter

from omnibase_core.models.triage import (
    EnumProbeStatus,
    ModelTriageProbeResult,
    ModelTriageReport,
    rank_findings,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_full_triage_orchestrator.handlers.probe_interface import (
    Probe,
)

_DEFAULT_PROBE_TIMEOUT_S = 30.0
_DEFAULT_MAX_WORKERS = 8


class ModelFullTriageRequest(BaseModel):
    """Input for the full triage orchestrator.

    An empty probes list produces an empty report — useful for smoke testing
    the aggregation layer without any concrete probe wired yet.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    probes: list[Probe] = Field(default_factory=list)
    probe_timeout_s: float = Field(
        default=_DEFAULT_PROBE_TIMEOUT_S,
        gt=0,
        description="Hard timeout per probe; enforced by the orchestrator",
    )
    max_workers: int = Field(
        default=_DEFAULT_MAX_WORKERS,
        gt=0,
        description="Thread pool size for parallel fan-out",
    )
    run_id: str | None = Field(
        default=None,
        description="Optional override; auto-generated UUID4 when None",
    )


class NodeFullTriageOrchestrator:
    """Aggregate read-only probe results into a ranked triage report."""

    def handle(self, request: ModelFullTriageRequest) -> ModelTriageReport:
        started_at = datetime.now(UTC)
        run_id = request.run_id or f"triage-{uuid.uuid4().hex[:12]}"

        results = self._run_probes(
            request.probes,
            timeout_s=request.probe_timeout_s,
            max_workers=request.max_workers,
        )

        all_findings = [f for r in results for f in r.findings]
        ranked = rank_findings(all_findings)

        return ModelTriageReport(
            run_id=run_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            probe_results=results,
            ranked_findings=ranked,
        )

    def _run_probes(
        self,
        probes: list[Probe],
        timeout_s: float,
        max_workers: int,
    ) -> list[ModelTriageProbeResult]:
        if not probes:
            return []

        results: list[ModelTriageProbeResult] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._run_one, p, timeout_s): p for p in probes}
            for future, probe in futures.items():
                try:
                    results.append(future.result(timeout=timeout_s + 5.0))
                except FuturesTimeoutError:
                    results.append(
                        ModelTriageProbeResult(
                            probe_name=probe.probe_name,
                            status=EnumProbeStatus.TIMEOUT,
                            duration_ms=int(timeout_s * 1000),
                            error_message=(
                                f"Probe exceeded orchestrator timeout of {timeout_s}s"
                            ),
                        )
                    )
                except Exception as exc:
                    results.append(
                        ModelTriageProbeResult(
                            probe_name=probe.probe_name,
                            status=EnumProbeStatus.ERROR,
                            duration_ms=0,
                            error_message=f"{type(exc).__name__}: {exc}",
                        )
                    )

        results.sort(key=lambda r: r.probe_name)
        return results

    def _run_one(self, probe: Probe, timeout_s: float) -> ModelTriageProbeResult:
        start = perf_counter()
        try:
            result = probe.run(timeout_s=timeout_s)
        except Exception as exc:
            elapsed_ms = int((perf_counter() - start) * 1000)
            return ModelTriageProbeResult(
                probe_name=probe.probe_name,
                status=EnumProbeStatus.ERROR,
                duration_ms=elapsed_ms,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        return result
