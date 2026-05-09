"""Context bundle generator — pure, deterministic, no I/O."""

from __future__ import annotations

import hashlib
import json

from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_request import (
    ModelContextBundleRequest,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_result import (
    EnumBundleStatus,
    ModelContextBundleResult,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_context_bundle import (
    EnumContextLevel,
    ModelContextBundleL0,
    ModelContextBundleL1,
    ModelContextBundleL2,
    ModelContextBundleL3,
    ModelContextBundleL4,
)


def _bundle_id(
    level: EnumContextLevel,
    bundle: (
        ModelContextBundleL0
        | ModelContextBundleL1
        | ModelContextBundleL2
        | ModelContextBundleL3
        | ModelContextBundleL4
    ),
) -> str:
    raw = json.dumps(
        {"level": level.value, "bundle": bundle.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class HandlerContextBundle:
    """Build a ModelContextBundle at the requested depth. Pure, no I/O."""

    def handle(self, request: ModelContextBundleRequest) -> ModelContextBundleResult:
        ts = request.task_state
        rc = request.run_context
        level = request.requested_level

        bundle: (
            ModelContextBundleL0
            | ModelContextBundleL1
            | ModelContextBundleL2
            | ModelContextBundleL3
            | ModelContextBundleL4
        )

        if level == EnumContextLevel.L0:
            bundle = ModelContextBundleL0(ticket_id=ts.ticket_id)

        elif level == EnumContextLevel.L1:
            bundle = ModelContextBundleL1(
                ticket_id=ts.ticket_id,
                title=ts.title,
                status=ts.status.value,
                assignee=ts.assignee,
                priority=ts.priority.value,
                labels=ts.labels,
            )

        elif level == EnumContextLevel.L2:
            bundle = ModelContextBundleL2(
                ticket_id=ts.ticket_id,
                title=ts.title,
                status=ts.status.value,
                assignee=ts.assignee,
                priority=ts.priority.value,
                labels=ts.labels,
                session_id=rc.session_id,
                agent_id=rc.agent_id,
                timestamp=rc.timestamp,
                worker_type=rc.worker_type,
                repo=rc.repo,
                branch=rc.branch,
                trigger_event=rc.trigger_event,
            )

        elif level == EnumContextLevel.L3:
            bundle = ModelContextBundleL3(
                ticket_id=ts.ticket_id,
                title=ts.title,
                status=ts.status.value,
                assignee=ts.assignee,
                priority=ts.priority.value,
                labels=ts.labels,
                session_id=rc.session_id,
                agent_id=rc.agent_id,
                timestamp=rc.timestamp,
                worker_type=rc.worker_type,
                repo=rc.repo,
                branch=rc.branch,
                trigger_event=rc.trigger_event,
                parent_ticket_id=ts.parent_ticket_id,
                related_ticket_ids=ts.related_ticket_ids,
            )

        else:
            bundle = ModelContextBundleL4(
                ticket_id=ts.ticket_id,
                title=ts.title,
                status=ts.status.value,
                assignee=ts.assignee,
                priority=ts.priority.value,
                labels=ts.labels,
                session_id=rc.session_id,
                agent_id=rc.agent_id,
                timestamp=rc.timestamp,
                worker_type=rc.worker_type,
                repo=rc.repo,
                branch=rc.branch,
                trigger_event=rc.trigger_event,
                parent_ticket_id=ts.parent_ticket_id,
                related_ticket_ids=ts.related_ticket_ids,
                historical_summary=request.historical_summary,
                prior_attempt_count=request.prior_attempt_count,
            )

        bid = _bundle_id(level, bundle)
        return ModelContextBundleResult(
            status=EnumBundleStatus.OK,
            bundle_id=bid,
            requested_level=level,
            achieved_level=bundle.level,
            bundle=bundle,
        )


__all__ = ["HandlerContextBundle"]
