# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, idempotent fleet projection and time-bound alerts."""

from collections.abc import Sequence
from datetime import UTC, datetime

from ..models import (
    EnumHostDriftVerdict as Verdict,
)
from ..models import (
    ModelHostDriftFleetHost,
    ModelHostDriftFleetOverlay,
    ModelHostDriftFleetView,
    ModelHostDriftHeartbeatEvent,
    ModelHostDriftResolvedEvent,
)

FleetEvent = ModelHostDriftHeartbeatEvent | ModelHostDriftResolvedEvent


def age_text(seconds: float) -> str:
    minutes = max(0, int(seconds)) // 60
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def fold_view(
    *,
    previous: ModelHostDriftFleetView | None,
    events: Sequence[FleetEvent],
    overlay: ModelHostDriftFleetOverlay,
    now: datetime,
    consumer_group: str,
    malformed: int = 0,
) -> ModelHostDriftFleetView:
    """Fold latest heartbeats and unique resolutions; malformed counts this read."""
    hosts = {host.host: host for host in previous.hosts} if previous else {}
    for key in overlay.hosts:
        hosts.setdefault(key, ModelHostDriftFleetHost(host=key))
    for event in events:
        host = hosts.get(event.host, ModelHostDriftFleetHost(host=event.host))
        if isinstance(event, ModelHostDriftHeartbeatEvent):
            if (
                host.last_heartbeat_at is not None
                and event.checked_at <= host.last_heartbeat_at
            ):
                continue
            hosts[event.host] = host.model_copy(
                update={
                    "last_heartbeat_at": event.checked_at,
                    "last_check_id": event.check_id,
                    "open": event.open,
                }
            )
        else:
            identity = f"{event.check_id}:{event.key}"
            if identity in host.resolved_ids:
                continue
            hosts[event.host] = host.model_copy(
                update={
                    "resolved_count": host.resolved_count + 1,
                    "last_resolved": (*host.last_resolved, event)[-20:],
                    "resolved_ids": (*host.resolved_ids, identity),
                }
            )
    alerts: list[str] = []
    reporting = 0
    within_bound = 0
    required = tuple(key for key in overlay.hosts if key not in overlay.read_only_hosts)
    for key in required:
        host = hosts[key]
        if host.last_heartbeat_at is None:
            alerts.append(f"{key} heartbeat: never reported (age unknown)")
        else:
            age = (now - host.last_heartbeat_at).total_seconds()
            if age > overlay.heartbeat_stale_seconds:
                alerts.append(f"{key} heartbeat: heartbeat stale {age_text(age)}")
            else:
                reporting += 1
        for item in host.open:
            age = (now - item.first_seen_at).total_seconds()
            reason = (
                "check failed"
                if item.verdict == Verdict.CHECK_FAILED
                else "drift past bound"
                if item.verdict == Verdict.DRIFT and age >= item.bound_seconds
                else ""
            )
            if reason:
                alerts.append(
                    f"{key} {item.key}: {reason}, {item.observed[:80]} (age {age_text(age)})"
                )
            elif item.verdict == Verdict.DRIFT:
                within_bound += 1
    alerts.sort(
        key=lambda alert: (
            0 if " heartbeat: " in alert else 1 if ": check failed," in alert else 2
        )
    )
    failed = next(
        (i for i, alert in enumerate(alerts) if ": check failed," in alert), None
    )
    if failed is not None and failed >= 4:
        alerts.insert(3, alerts.pop(failed))
    stamp = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    line = (
        " ".join(
            f"host-drift: ALERT {len(alerts)} — {'; '.join(alerts[:4])} as of {stamp}".splitlines()
        )
        if alerts
        else f"host-drift: OK {reporting}/{len(required)} hosts reporting, {within_bound} open drift within bound as of {stamp}"
    )
    return ModelHostDriftFleetView(
        hosts=tuple(hosts[key] for key in sorted(hosts)),
        alerts=tuple(alerts),
        status_line=line,
        folded_at=now,
        consumer_group=consumer_group,
        malformed=malformed,
    )
