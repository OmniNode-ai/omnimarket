# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch-worker execution effect.

Consumes compiled dispatch-worker specs, builds runtime delegation payloads,
and writes idempotency receipts. It does not shell out, call Agent/TaskCreate
directly, or publish to the event bus.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.nodes.node_dispatch_worker_execution_effect.models import (
    EnumDispatchWorkerExecutionStatus,
    ModelDispatchWorkerDelegationPayload,
    ModelDispatchWorkerExecutionInput,
    ModelDispatchWorkerExecutionOutcome,
    ModelDispatchWorkerExecutionResult,
    ModelDispatchWorkerSpecArtifact,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_DELEGATION_TOPIC_SUFFIX = "delegation-request"
_DELEGATION_EVENT_TYPE = "omnimarket.dispatch-worker-execution-request"


def _load_delegation_topic() -> str:
    if not _CONTRACT_PATH.exists():
        msg = f"contract.yaml not found at {_CONTRACT_PATH}"
        raise RuntimeError(msg)

    with _CONTRACT_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    publish_topics: list[str] = (data.get("event_bus", {}) or {}).get(
        "publish_topics", []
    ) or []
    for topic in publish_topics:
        if _DELEGATION_TOPIC_SUFFIX in topic:
            return topic

    msg = (
        f"contract.yaml at {_CONTRACT_PATH} does not declare a publish topic "
        f"containing {_DELEGATION_TOPIC_SUFFIX!r}"
    )
    raise RuntimeError(msg)


_TOPIC_DELEGATION_REQUEST = _load_delegation_topic()


class HandlerDispatchWorkerExecution:
    """Build delegation payloads from compiled dispatch-worker specs."""

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    def handle(
        self, command: ModelDispatchWorkerExecutionInput
    ) -> ModelDispatchWorkerExecutionResult:
        specs = self._load_specs(command)
        self._reject_duplicate_specs(specs)

        outcomes: list[ModelDispatchWorkerExecutionOutcome] = []
        delegation_payloads: list[ModelDispatchWorkerDelegationPayload] = []
        total_delegated = 0
        total_failed = 0
        total_rejected = 0
        total_skipped = 0

        receipt_dir = Path(command.resolved_receipt_dir)

        for spec in specs:
            receipt_path = self._receipt_path(receipt_dir, spec)

            if command.dry_run:
                total_skipped += 1
                outcomes.append(
                    self._outcome(
                        spec,
                        status=EnumDispatchWorkerExecutionStatus.DRY_RUN,
                        receipt_path="",
                    )
                )
                continue

            if spec.dispatch_worker.rejected_reason:
                total_rejected += 1
                outcomes.append(
                    self._outcome(
                        spec,
                        status=EnumDispatchWorkerExecutionStatus.REJECTED,
                        error=spec.dispatch_worker.rejected_reason,
                    )
                )
                continue

            try:
                payload = self._build_delegation_payload(spec, command)
                try:
                    self._write_receipt(receipt_path, spec, payload)
                except FileExistsError:
                    total_skipped += 1
                    outcomes.append(
                        self._outcome(
                            spec,
                            status=EnumDispatchWorkerExecutionStatus.SKIPPED_DUPLICATE,
                            receipt_path=str(receipt_path),
                        )
                    )
                    continue
                delegation_payloads.append(payload)
                total_delegated += 1
                outcomes.append(
                    self._outcome(
                        spec,
                        status=EnumDispatchWorkerExecutionStatus.DELEGATED,
                        delegated=True,
                        receipt_path=str(receipt_path),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Dispatch-worker execution failed for %s/%s: %s",
                    spec.session_id,
                    spec.dispatch_id,
                    exc,
                    exc_info=True,
                )
                total_failed += 1
                outcomes.append(
                    self._outcome(
                        spec,
                        status=EnumDispatchWorkerExecutionStatus.FAILED,
                        error=str(exc),
                    )
                )

        return ModelDispatchWorkerExecutionResult(
            correlation_id=command.correlation_id,
            outcomes=tuple(outcomes),
            total_delegated=total_delegated,
            total_failed=total_failed,
            total_rejected=total_rejected,
            total_skipped=total_skipped,
            delegation_payloads=tuple(delegation_payloads),
        )

    def _load_specs(
        self, command: ModelDispatchWorkerExecutionInput
    ) -> list[ModelDispatchWorkerSpecArtifact]:
        specs = list(command.artifacts)
        for path_raw in command.artifact_paths:
            path = Path(path_raw)
            with path.open(encoding="utf-8") as fh:
                specs.append(
                    ModelDispatchWorkerSpecArtifact.model_validate(json.load(fh))
                )
        return specs

    @staticmethod
    def _reject_duplicate_specs(specs: list[ModelDispatchWorkerSpecArtifact]) -> None:
        seen: set[tuple[str, str, str]] = set()
        for spec in specs:
            key = (spec.session_id, spec.dispatch_id, spec.ticket_id)
            if key in seen:
                msg = f"Duplicate dispatch-worker spec in batch: {key!r}"
                raise ValueError(msg)
            seen.add(key)

    def _build_delegation_payload(
        self,
        spec: ModelDispatchWorkerSpecArtifact,
        command: ModelDispatchWorkerExecutionInput,
    ) -> ModelDispatchWorkerDelegationPayload:
        worker = spec.dispatch_worker
        now = datetime.now(tz=UTC).isoformat()
        payload: dict[str, object] = {
            "task_type": "agent_dispatch",
            "task_description": worker.validated_task_description,
            "prompt": worker.validated_prompt_template,
            "agent_spawn_args": dict(worker.proposed_agent_spawn_args),
            "session_id": spec.session_id,
            "ticket_id": spec.ticket_id,
            "dispatch_id": spec.dispatch_id,
            "correlation_id": str(command.correlation_id),
            "correlation_chain": spec.correlation_chain,
            "collision_fences": list(worker.collision_fence_embeds),
            "source": "node_dispatch_worker_execution_effect",
            "emitted_at": now,
        }
        return ModelDispatchWorkerDelegationPayload(
            event_type=_DELEGATION_EVENT_TYPE,
            topic=_TOPIC_DELEGATION_REQUEST,
            payload=payload,
            correlation_id=command.correlation_id,
        )

    @staticmethod
    def _receipt_path(receipt_dir: Path, spec: ModelDispatchWorkerSpecArtifact) -> Path:
        safe_ticket = _safe_segment(spec.ticket_id.lower())
        safe_dispatch = _safe_segment(spec.dispatch_id.lower())
        safe_session = _safe_segment(spec.session_id.lower())
        return receipt_dir / f"{safe_session}-{safe_dispatch}-{safe_ticket}.json"

    @staticmethod
    def _write_receipt(
        receipt_path: Path,
        spec: ModelDispatchWorkerSpecArtifact,
        payload: ModelDispatchWorkerDelegationPayload,
    ) -> None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "session_id": spec.session_id,
            "ticket_id": spec.ticket_id,
            "dispatch_id": spec.dispatch_id,
            "correlation_chain": spec.correlation_chain,
            "status": EnumDispatchWorkerExecutionStatus.DELEGATED.value,
            "delegation_topic": payload.topic,
            "delegation_event_type": payload.event_type,
            "written_at": datetime.now(tz=UTC).isoformat(),
        }
        with receipt_path.open("x", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2)
            fh.write("\n")

    @staticmethod
    def _outcome(
        spec: ModelDispatchWorkerSpecArtifact,
        *,
        status: EnumDispatchWorkerExecutionStatus,
        delegated: bool = False,
        error: str = "",
        receipt_path: str = "",
    ) -> ModelDispatchWorkerExecutionOutcome:
        return ModelDispatchWorkerExecutionOutcome(
            session_id=spec.session_id,
            ticket_id=spec.ticket_id,
            dispatch_id=spec.dispatch_id,
            status=status,
            delegated=delegated,
            error=error,
            receipt_path=receipt_path,
        )


def _safe_segment(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value).strip("-")


__all__ = [
    "_DELEGATION_EVENT_TYPE",
    "_TOPIC_DELEGATION_REQUEST",
    "HandlerDispatchWorkerExecution",
]
