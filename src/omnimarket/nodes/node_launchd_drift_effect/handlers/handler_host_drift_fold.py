# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure per-host drift transitions (OMN-17427)."""

from __future__ import annotations

from ..models.enum_host_drift_resolution import EnumHostDriftResolution
from ..models.enum_host_drift_verdict import EnumHostDriftVerdict as Verdict
from ..models.model_host_drift_detected_event import ModelHostDriftDetectedEvent
from ..models.model_host_drift_fold_request import ModelHostDriftFoldRequest
from ..models.model_host_drift_fold_result import ModelHostDriftFoldResult
from ..models.model_host_drift_heartbeat_event import ModelHostDriftHeartbeatEvent
from ..models.model_host_drift_item import ModelHostDriftItem
from ..models.model_host_drift_open_item import ModelHostDriftOpenItem
from ..models.model_host_drift_resolved_event import ModelHostDriftResolvedEvent
from ..models.model_host_drift_state import ModelHostDriftState


class HandlerHostDriftFold:
    def handle(self, request: ModelHostDriftFoldRequest) -> ModelHostDriftFoldResult:
        previous, observation = request.previous, request.observation
        if previous.host != observation.host:
            raise ValueError("state host does not match observation")
        now = observation.observed_at
        gap = (
            int((now - previous.last_check_at).total_seconds())
            if previous.last_check_at
            else None
        )
        items = list(observation.items)
        if gap is not None and gap > 2 * request.interval_seconds:
            items.append(
                ModelHostDriftItem(
                    "heartbeat:gap",
                    "heartbeat",
                    Verdict.DRIFT,
                    f"check every {request.interval_seconds}s",
                    f"no check for {gap // 60}m",
                    0,
                )
            )
        if request.checked_keys is not None:
            items = [item for item in items if item.key in request.checked_keys]
        old = {item.key: item for item in previous.open}
        opened: list[ModelHostDriftOpenItem] = [
            item
            for item in previous.open
            if request.checked_keys is not None and item.key not in request.checked_keys
        ]
        detected: list[ModelHostDriftDetectedEvent] = []
        for item in items:
            if item.verdict == Verdict.OK:
                continue
            first = old[item.key].first_seen_at if item.key in old else now
            opened.append(
                ModelHostDriftOpenItem(
                    item.key,
                    item.kind,
                    item.declared,
                    item.observed,
                    first,
                    item.bound_seconds,
                    item.verdict,
                )
            )
            if item.key not in old:
                detected.append(
                    ModelHostDriftDetectedEvent(
                        observation.host,
                        observation.role,
                        request.check_id,
                        item.key,
                        item.kind,
                        item.verdict,
                        item.declared,
                        item.observed,
                        first,
                        now,
                        item.bound_seconds,
                    )
                )
        still_open = {item.key for item in opened}
        resolved = tuple(
            ModelHostDriftResolvedEvent(
                observation.host,
                observation.role,
                request.check_id,
                item.key,
                item.kind,
                item.declared,
                item.observed,
                item.first_seen_at,
                now,
                max(0, int((now - item.first_seen_at).total_seconds())),
                EnumHostDriftResolution.OBSERVED_CONVERGED,
            )
            for item in previous.open
            if item.key not in still_open
        )
        state = ModelHostDriftState(
            observation.host, now, tuple(opened), request.generation
        )
        heartbeat = ModelHostDriftHeartbeatEvent(
            observation.host,
            observation.role,
            request.check_id,
            now,
            request.interval_seconds,
            len(items),
            sum(i.verdict == Verdict.OK for i in items),
            sum(i.verdict == Verdict.DRIFT for i in items),
            sum(i.verdict == Verdict.CHECK_FAILED for i in items),
            state.open,
            previous.last_check_at,
            gap,
            generation=request.generation if request.heartbeat_extensions else None,
        )
        return ModelHostDriftFoldResult(state, tuple(detected), resolved, heartbeat)
