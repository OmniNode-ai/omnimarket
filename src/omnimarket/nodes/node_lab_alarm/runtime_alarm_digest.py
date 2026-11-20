# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``alarm-digest`` and ``alarm-digest-probe`` verbs: the lab alarm's daily digest step (OMN-24939).

``alarm-digest`` posts one message per held day before today and appends the day's fix-cause STATUS row;
``alarm-digest-probe`` names what is due and sends nothing. Its schedule is the overlay's
``condition_schedules.alarm-digest`` block (interval 86400). Exit 0: sent or nothing due; 1: refused (no delivery
turned on, or an overlay that does not load); 2: a post or the STATUS row failed, retried next run.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_alarm.adapters.routed_delivery_adapters import (
    build_alarm_digest,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    EnumAlarmDigestOutcome,
    ModelAlarmDigestCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    load_overlay as load_delivery_overlay,
)

DIGEST_VERBS = ("alarm-digest", "alarm-digest-probe")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_alarm_digest(
    verb: str,
    *,
    overlay: ModelLabAlarmOverlay,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    if overlay.alarm_delivery is None or not overlay.alarm_delivery.enabled:
        sys.stderr.write(
            "LAB-ALARM-REFUSED alarm-digest: the overlay turns no alarm delivery on\n"
        )
        return 1
    try:
        handler = build_alarm_digest(load_delivery_overlay(), clock=clock)
    except (NodeOverlayError, ValueError) as exc:
        sys.stderr.write(f"LAB-ALARM-REFUSED alarm-digest: {exc}\n")
        return 1
    result = handler.handle(
        ModelAlarmDigestCommand(
            correlation_id=f"lab-alarm-digest-{clock():%Y%m%dT%H%M%SZ}",
            write=verb == "alarm-digest",
        )
    )
    for day in result.days:
        sys.stdout.write(
            f"DIGEST day={day.day} episodes={day.episodes} sources={day.sources} "
            f"ref={day.message_ref or '-'}{' failure=' + day.failure if day.failure else ''}\n"
        )
    sys.stdout.write(
        f"LAB-ALARM-DIGEST outcome={result.outcome.value} status_row={result.status_row} "
        f"req={result.status_request_id or '-'}\n"
    )
    if result.outcome is EnumAlarmDigestOutcome.FAILED:
        sys.stderr.write(f"LAB-ALARM-DIGEST failure={result.failure}\n")
        return 2
    return 0


__all__ = ["DIGEST_VERBS", "run_alarm_digest"]
