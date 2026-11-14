# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Windows and run ids of the weekly coding test (OMN-24177, plan task T13).

Window k of an arm that runs every ``days`` days runs from ``epoch + k * days`` to
``epoch + (k + 1) * days``, counted from the record's epoch and never from a deployment or the
last run. A tick is in exactly one window, so a tick that arrives early reads the window before
it (the run it already has), a tick after a gap reads the window it falls in, and a window the
clock jumped over has no tick and is never made up. The run id is a uuid5 of the record, the
model, the cadence and the window index: the same window always names the same run, so a send
that is repeated after a lost send or a restart names a run the trial driver either holds
(and drops) or has never started (and starts once).
"""

from __future__ import annotations

import datetime as dt
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.models.eval_tracking.enum_eval_tracking_cadence import (
    EnumEvalTrackingCadence,
)
from omnimarket.models.eval_tracking.model_eval_tracking_overlay import (
    ModelEvalTrackingOverlay,
)
from omnimarket.models.eval_tracking.model_eval_tracking_record import (
    ModelEvalTrackingArm,
)

_RUN_NAMESPACE = uuid5(NAMESPACE_URL, "onex:omnimarket:eval-tracking-run:v1")


def cadence_days(overlay: ModelEvalTrackingOverlay, arm: ModelEvalTrackingArm) -> int:
    """The days one window of this arm lasts."""
    if arm.cadence is EnumEvalTrackingCadence.GLM:
        return overlay.glm_days
    return overlay.local_days


def window_index(now: dt.datetime, epoch: dt.datetime, days: int) -> int | None:
    """The window ``now`` falls in, or None before the epoch (no window is open yet)."""
    elapsed = now.astimezone(dt.UTC) - epoch.astimezone(dt.UTC)
    if elapsed < dt.timedelta(0):
        return None
    return elapsed // dt.timedelta(days=days)


def tracking_run_id(record_sha256: str, model: str, days: int, window: int) -> UUID:
    """The run of one model in one window of one record."""
    return uuid5(_RUN_NAMESPACE, f"{record_sha256}|{model}|{days}|{window}")


def arm_for_slot(slot: int, arm_count: int, resend_ticks: int) -> int | None:
    """The arm whose turn this tick slot is, or None when it is nobody's.

    Every ``resend_ticks`` slots the turn passes to the next arm, so one tick names at most one
    run and each arm is named once every ``resend_ticks * arm_count`` slots.
    """
    if slot % resend_ticks != 0:
        return None
    return (slot // resend_ticks) % arm_count


__all__ = ["arm_for_slot", "cadence_days", "tracking_run_id", "window_index"]
