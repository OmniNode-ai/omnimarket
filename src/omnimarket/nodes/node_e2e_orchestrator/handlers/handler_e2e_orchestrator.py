# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the build-loop to PR-lifecycle bridge.

``handle(request)`` takes one event and returns the commands the runtime publishes, each to the topic
the contract's ``published_events`` names. It reads no environment, runs no process, touches no file and
has no clock: the CI read and the evidence writes are node_e2e_bridge_effect's, and every instant the
decision compares comes from the events.

* build loop completed: record the run, then read CI (no PR refs: nothing to wait for, start the
  lifecycle at once).
* CI read completed: all green starts the PR lifecycle; any red or unparseable ref stops; anything else
  is not green yet, so another read is requested after the poll interval until the deadline.
* PR lifecycle completed: write the merge evidence and the receipts.

The commands leave through the contract's ``event_bus.publish_topics``, each mapped to its topic by
class name in ``published_events``. The compose runtime drops a bare list returned from ``handle`` while
its fan-out seam flag is off, so it enters through :meth:`HandlerE2eOrchestrator.handle_async`, which
returns the same commands as a ``ModelHandlerOutput`` the runtime does publish.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel

from omnimarket.models.e2e_bridge import (
    ModelE2eCiReadCompleted,
    ModelE2eCiReadRequest,
    ModelE2eEvidenceFile,
    ModelE2eEvidenceWriteRequest,
    ModelE2ePrLifecycleStart,
)
from omnimarket.nodes.node_e2e_orchestrator.models import (
    ModelE2eBuildCompleted,
    ModelE2ePrLifecycleCompleted,
)

_HANDLER_ID = "node_e2e_orchestrator.bridge"
_ENVELOPE_NAMESPACE = UUID("6f0f3d1e-8f0a-5b6e-9c1d-2e2b7d6c4a10")
_PHASES = ["build_completed", "ci_poll", "merge_sweep"]
_STOPPING = {"red", "unparseable"}

E2eRequest = (
    ModelE2eBuildCompleted | ModelE2eCiReadCompleted | ModelE2ePrLifecycleCompleted
)


def _file(filename: str, data: dict[str, object]) -> ModelE2eEvidenceFile:
    return ModelE2eEvidenceFile(filename=filename, data=data)


def _start(correlation_id: UUID, *, dry_run: bool) -> ModelE2ePrLifecycleStart:
    return ModelE2ePrLifecycleStart(
        correlation_id=correlation_id,
        run_id=f"e2e-{correlation_id}",
        dry_run=dry_run,
    )


class HandlerE2eOrchestrator:
    """ORCHESTRATOR (definition B): one event in, the commands it calls for out."""

    async def handle_async(self, request: E2eRequest) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the decision's commands as publishable handler output."""
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid5(
                _ENVELOPE_NAMESPACE,
                f"{request.correlation_id}|{type(request).__name__}",
            ),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            events=tuple(self.handle(request)),
        )

    def handle(self, request: E2eRequest) -> list[BaseModel]:
        if isinstance(request, ModelE2eBuildCompleted):
            return self._build_completed(request)
        if isinstance(request, ModelE2eCiReadCompleted):
            return self._ci_read_completed(request)
        if isinstance(request, ModelE2ePrLifecycleCompleted):
            return self._lifecycle_completed(request)
        raise TypeError(f"unsupported e2e request {type(request).__name__}")

    def _build_completed(self, event: ModelE2eBuildCompleted) -> list[BaseModel]:
        cid = event.correlation_id
        pr_refs = list(event.pr_refs)
        files = [
            _file("build_completed.json", event.model_dump(mode="json")),
            _file("pr_refs.json", {"pr_refs": pr_refs}),
            _file(
                "cost_event_refs.json", {"cost_event_keys": list(event.cost_event_keys)}
            ),
            _file(
                "run_manifest.json",
                {
                    "correlation_id": str(cid),
                    "started_at": (
                        event.completed_at.isoformat() if event.completed_at else None
                    ),
                    "pr_refs": pr_refs,
                    "phases": _PHASES,
                },
            ),
        ]
        out: list[BaseModel] = []
        if not pr_refs:
            files.append(_file("ci_checks.json", {"checks": [], "all_green": True}))
            out.append(
                ModelE2eEvidenceWriteRequest(correlation_id=cid, files=tuple(files))
            )
            out.append(_start(cid, dry_run=event.dry_run))
            return out
        out.append(ModelE2eEvidenceWriteRequest(correlation_id=cid, files=tuple(files)))
        deadline = (
            event.completed_at + timedelta(seconds=event.ci_poll_timeout_seconds)
            if event.completed_at
            else None
        )
        out.append(
            ModelE2eCiReadRequest(
                correlation_id=cid,
                pr_refs=event.pr_refs,
                attempt=0,
                deadline_at=deadline,
                timeout_seconds=event.ci_poll_timeout_seconds,
                interval_seconds=event.ci_poll_interval_seconds,
                dry_run=event.dry_run,
            )
        )
        return out

    def _ci_read_completed(self, done: ModelE2eCiReadCompleted) -> list[BaseModel]:
        cid = done.correlation_id
        checks = [r.model_dump(mode="json") for r in done.readings]
        if all(r.overall == "green" for r in done.readings):
            return [
                _ci_evidence(cid, checks, all_green=True, status="green"),
                _start(cid, dry_run=done.dry_run),
            ]
        if any(r.overall in _STOPPING for r in done.readings):
            return [_ci_evidence(cid, checks, all_green=False, status="failed")]
        deadline = done.deadline_at or done.read_at + timedelta(
            seconds=done.timeout_seconds
        )
        if done.read_at >= deadline:
            return [_ci_evidence(cid, checks, all_green=False, status="timeout")]
        return [
            ModelE2eCiReadRequest(
                correlation_id=cid,
                pr_refs=done.pr_refs,
                attempt=done.attempt + 1,
                not_before=_after(done.read_at, done.interval_seconds),
                deadline_at=deadline,
                timeout_seconds=done.timeout_seconds,
                interval_seconds=done.interval_seconds,
                dry_run=done.dry_run,
            )
        ]

    def _lifecycle_completed(
        self, event: ModelE2ePrLifecycleCompleted
    ) -> list[BaseModel]:
        cid = event.correlation_id
        receipts = {
            "correlation_id": str(cid),
            "merge_completed": True,
            "prs_merged": event.prs_merged,
            "prs_fixed": event.prs_fixed,
            "final_state": event.final_state,
        }
        return [
            ModelE2eEvidenceWriteRequest(
                correlation_id=cid,
                files=(
                    _file("merge_completed.json", event.model_dump(mode="json")),
                    _file("receipts.json", receipts),
                ),
            )
        ]


def _after(instant: datetime, seconds: int) -> datetime:
    return instant + timedelta(seconds=seconds)


def _ci_evidence(
    correlation_id: UUID,
    checks: list[dict[str, object]],
    *,
    all_green: bool,
    status: str,
) -> ModelE2eEvidenceWriteRequest:
    return ModelE2eEvidenceWriteRequest(
        correlation_id=correlation_id,
        files=(
            _file(
                "ci_checks.json",
                {"checks": checks, "all_green": all_green, "status": status},
            ),
        ),
    )


__all__: list[str] = ["E2eRequest", "HandlerE2eOrchestrator"]
