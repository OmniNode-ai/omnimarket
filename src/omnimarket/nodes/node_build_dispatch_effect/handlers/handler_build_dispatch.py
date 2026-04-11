# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that dispatches ticket-pipeline builds via delegation.

This is an EFFECT handler - performs external I/O (delegation dispatch).

Architectural rule: effect handlers must NOT have direct event bus access.
Instead, this handler builds delegation request payloads and returns them
in the result.  The orchestrator is responsible for publishing them to Kafka.

Behavior change (OMN-7582): filesystem fallback REMOVED. Kafka is the
canonical transport. ModelInfraErrorContext removed in favor of structured
logging with the same correlation context.

Related:
    - OMN-7582: Migrate node_build_dispatch_effect to omnimarket
    - OMN-7381: Wire handler_build_dispatch to delegation orchestrator
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml

from omnimarket.nodes.node_build_dispatch_effect.handlers.dispatch_history_store import (
    DispatchHistoryStore,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_outcome import (
    ModelBuildDispatchOutcome,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_dispatch_result import (
    ModelBuildDispatchResult,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_build_target import (
    ModelBuildTarget,
)
from omnimarket.nodes.node_build_dispatch_effect.models.model_delegation_payload import (
    ModelDelegationPayload,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler type / category as Literal types (replaces infra enums)
# ---------------------------------------------------------------------------
HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]

# ---------------------------------------------------------------------------
# Resolve delegation topic from contract.yaml (single source of truth)
# ---------------------------------------------------------------------------
_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_DELEGATION_TOPIC_SUFFIX = "delegation-request"


def _load_delegation_topic() -> str:
    """Load the delegation-request publish topic from contract.yaml.

    Raises:
        RuntimeError: If contract.yaml is missing or does not declare a
            publish topic containing 'delegation-request'.
    """
    if not _CONTRACT_PATH.exists():
        msg = f"contract.yaml not found at {_CONTRACT_PATH}"
        raise RuntimeError(msg)

    with open(_CONTRACT_PATH) as fh:
        data = yaml.safe_load(fh) or {}

    event_bus = data.get("event_bus", {}) or {}
    publish_topics: list[str] = event_bus.get("publish_topics", []) or []

    for topic in publish_topics:
        if _DELEGATION_TOPIC_SUFFIX in topic:
            return topic

    msg = (
        f"contract.yaml at {_CONTRACT_PATH} does not declare a "
        f"publish topic containing {_DELEGATION_TOPIC_SUFFIX!r}"
    )
    raise RuntimeError(msg)


_TOPIC_DELEGATION_REQUEST: str = _load_delegation_topic()

# Event type used by the delegation dispatcher for message routing.
# Must match DispatcherDelegationRequest.message_types.
_DELEGATION_EVENT_TYPE = "omnimarket.delegation-request"


class HandlerBuildDispatch:
    """Dispatches ticket-pipeline builds for AUTO_BUILDABLE tickets via delegation.

    Primary path: builds ``ModelDelegationPayload`` objects for each ticket
    and returns them in the result.  The orchestrator publishes these to
    Kafka (architectural rule: only orchestrators may access the event bus).

    Failures on individual tickets do not block other dispatches.
    """

    def __init__(
        self,
        *,
        history_store: DispatchHistoryStore | None = None,
    ) -> None:
        self._history_store = history_store or DispatchHistoryStore()

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    async def handle(
        self,
        correlation_id: UUID,
        targets: tuple[ModelBuildTarget, ...],
        dry_run: bool = False,
    ) -> ModelBuildDispatchResult:
        """Dispatch builds for each target ticket.

        Args:
            correlation_id: Cycle correlation ID.
            targets: Tickets to dispatch.
            dry_run: Skip actual dispatch.

        Returns:
            ModelBuildDispatchResult with per-ticket outcomes and delegation
            payloads for the orchestrator to publish.
        """
        logger.info(
            "Build dispatch: %d targets (correlation_id=%s, dry_run=%s)",
            len(targets),
            correlation_id,
            dry_run,
        )

        outcomes: list[ModelBuildDispatchOutcome] = []
        delegation_payloads: list[ModelDelegationPayload] = []
        total_dispatched = 0
        total_failed = 0

        seen_ticket_ids: set[str] = set()
        for target in targets:
            if target.ticket_id in seen_ticket_ids:
                msg = f"Duplicate ticket_id in dispatch batch: {target.ticket_id!r}"
                raise ValueError(msg)
            seen_ticket_ids.add(target.ticket_id)

        for target in targets:
            if dry_run:
                outcomes.append(
                    ModelBuildDispatchOutcome(
                        ticket_id=target.ticket_id,
                        dispatched=True,
                        error=None,
                    )
                )
                total_dispatched += 1
                continue

            try:
                payload = self._build_delegation_payload(
                    target=target,
                    correlation_id=correlation_id,
                )
                delegation_payloads.append(payload)
                try:
                    record = self._history_store.record_dispatch(
                        ticket_id=target.ticket_id,
                        correlation_id=str(correlation_id),
                    )
                    logger.info(
                        "dispatch_history: recorded %s (attempt %d)",
                        target.ticket_id,
                        record.attempt_count,
                    )
                except OSError as store_exc:
                    logger.warning(
                        "dispatch_history: failed to record %s: %s",
                        target.ticket_id,
                        store_exc,
                    )
                logger.info(
                    "Dispatched ticket-pipeline for %s: %s",
                    target.ticket_id,
                    target.title,
                )
                outcomes.append(
                    ModelBuildDispatchOutcome(
                        ticket_id=target.ticket_id,
                        dispatched=True,
                        error=None,
                    )
                )
                total_dispatched += 1
            except Exception as exc:
                logger.warning(
                    "Failed to dispatch %s: %s "
                    "(correlation_id=%s, transport=kafka, operation=delegation_payload_build)",
                    target.ticket_id,
                    exc,
                    correlation_id,
                    exc_info=True,
                )
                outcomes.append(
                    ModelBuildDispatchOutcome(
                        ticket_id=target.ticket_id,
                        dispatched=False,
                        error=str(exc),
                    )
                )
                total_failed += 1

        logger.info(
            "Build dispatch complete: %d dispatched, %d failed",
            total_dispatched,
            total_failed,
        )

        return ModelBuildDispatchResult(
            correlation_id=correlation_id,
            outcomes=tuple(outcomes),
            total_dispatched=total_dispatched,
            total_failed=total_failed,
            delegation_payloads=tuple(delegation_payloads),
        )

    # ------------------------------------------------------------------
    # Primary dispatch: build delegation payload (orchestrator publishes)
    # ------------------------------------------------------------------

    def _build_delegation_payload(
        self,
        *,
        target: ModelBuildTarget,
        correlation_id: UUID,
    ) -> ModelDelegationPayload:
        """Build a delegation request payload for a single ticket.

        Returns a ``ModelDelegationPayload`` that the orchestrator will
        publish to the delegation-request Kafka topic.
        """
        now = datetime.now(tz=UTC)
        payload: dict[str, object] = {
            "prompt": f"Run ticket-pipeline for {target.ticket_id}",
            "task_type": "research",
            "source_session_id": None,
            "source_file_path": None,
            "correlation_id": str(correlation_id),
            "max_tokens": 4096,
            "emitted_at": now.isoformat(),
        }

        return ModelDelegationPayload(
            event_type=_DELEGATION_EVENT_TYPE,
            topic=_TOPIC_DELEGATION_REQUEST,
            payload=payload,
            correlation_id=correlation_id,
        )


__all__: list[str] = ["HandlerBuildDispatch"]
