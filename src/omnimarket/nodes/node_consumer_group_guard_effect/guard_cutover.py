# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check supplied cutover readbacks without installing or stopping any unit."""

from __future__ import annotations

import re
from datetime import timedelta
from itertools import pairwise

from pydantic import ValidationError

from .guard_cutover_models import ModelGuardCutoverEvidence
from .guard_schedule_models import ModelGuardScheduleResult


def verify_cutover(
    data: object, *, host: str, interval_seconds: int
) -> ModelGuardScheduleResult:
    """Refuse missing, inconsistent or out-of-order host readbacks.

    This is a read of previously captured evidence, not a live host probe. It
    makes no claim that supplied readbacks originated on the named machines.
    """
    try:
        evidence = ModelGuardCutoverEvidence.model_validate(data)
    except ValidationError as exc:
        return ModelGuardScheduleResult(
            exit_code=1, stderr=f"REFUSED: invalid cutover readbacks: {exc}\n"
        )
    gaps = _gaps(evidence, host=host, interval_seconds=interval_seconds)
    if gaps:
        return ModelGuardScheduleResult(
            exit_code=1, stderr="".join(f"REFUSED: {gap}\n" for gap in gaps)
        )
    return ModelGuardScheduleResult(
        exit_code=0,
        stdout=f"{host}: supplied readbacks satisfy cutover order, parity and rollback\n",
    )


def _gaps(
    evidence: ModelGuardCutoverEvidence, *, host: str, interval_seconds: int
) -> list[str]:
    gaps: list[str] = []
    if evidence.host != host:
        gaps.append(f"readback host {evidence.host!r} differs from {host!r}")
    if interval_seconds <= 0:
        gaps.append("cadence interval must be positive")
    if len(set(evidence.implementation_digests.model_dump().values())) != 1:
        gaps.append("Mac clone, lab clone and dispatch implementation digests differ")
    if evidence.other_unit_name in (
        "consumer-group-guard",
        "onex-consumer-group-guard",
    ):
        gaps.append("other unit installer must name a different unit")
    successful = (
        evidence.mac_unload,
        evidence.lab_install,
        evidence.host_reconcile,
        evidence.other_unit_install,
        evidence.rollback_uninstall,
        evidence.rollback_stop,
        evidence.rollback_reset_failed,
        evidence.mac_reinstall,
    )
    if any(readback.exit_code != 0 for readback in successful):
        gaps.append("an unload, install, reconcile or rollback command failed")
    for readback in (
        evidence.mac_absent_after_unload,
        evidence.mac_absent_after_reconcile,
        evidence.mac_absent_after_other_install,
    ):
        if readback.exit_code <= 0:
            gaps.append("Mac launchctl label is still present")
    timeline = (
        evidence.mac_unload,
        evidence.mac_absent_after_unload,
        evidence.lab_install,
        evidence.host_reconcile,
        evidence.mac_absent_after_reconcile,
        evidence.other_unit_install,
        evidence.mac_absent_after_other_install,
    )
    if any(a.observed_at >= b.observed_at for a, b in pairwise(timeline)):
        gaps.append(
            "Mac unload, absence, lab install and subsequent absence reads are out of order"
        )
    runs = evidence.runs
    if len({run.invocation_id for run in runs}) != len(runs):
        gaps.append("timer invocations are not distinct")
    if any(a.completed_at >= b.started_at for a, b in pairwise(runs)):
        gaps.append("timer runs overlap or are not ordered by start time")
    if (
        runs[-1].started_at - runs[0].started_at
    ).total_seconds() < 2 * interval_seconds:
        gaps.append("timer invocations are less than two cadence intervals apart")
    install_time = evidence.lab_install.observed_at
    reaped = False
    for run in runs:
        if not (
            install_time
            < run.last_trigger_at
            <= run.started_at
            <= run.observed_at
            <= run.completed_at
            and run.started_at
            < run.completed_at
            <= install_time + timedelta(minutes=60)
        ):
            gaps.append(
                f"{run.invocation_id}: timer, journal and census times disagree or exceed 60 minutes"
            )
        # LastTriggerUSec belongs to this invocation, rather than an old tick.
        if (run.started_at - run.last_trigger_at).total_seconds() >= interval_seconds:
            gaps.append(
                f"{run.invocation_id}: timer trigger belongs to an earlier cadence"
            )
        if (
            run.broker != evidence.mac_baseline.broker
            or run.bus_lane != evidence.mac_baseline.bus_lane
        ):
            gaps.append(
                f"{run.invocation_id}: broker or bus lane differs from Mac baseline"
            )
        counts = re.fullmatch(
            r"reaped (\d+) of (\d+) eligible(?: Empty groups)?", run.reap_detail
        )
        if counts and int(counts[1]) == int(counts[2]):
            reaped = reaped or int(counts[1]) >= 1
        elif not re.fullmatch(
            r"nothing to reap among \d+ Empty groups", run.reap_detail
        ):
            gaps.append(f"{run.invocation_id}: reap is incomplete")
    if not reaped:
        gaps.append("no timer run reaped N of N with N at least one")
    inactive = evidence.rollback_is_active
    if inactive.exit_code != 3 or inactive.stdout.strip() != "inactive":
        gaps.append("rollback is-active did not read inactive (exit 3)")
    if timeline[-1].observed_at >= evidence.rollback_uninstall.observed_at:
        gaps.append("Mac absence reads must precede lab rollback")
    rollback = (
        evidence.rollback_uninstall,
        evidence.rollback_stop,
        evidence.rollback_reset_failed,
        inactive,
        evidence.mac_reinstall,
    )
    if max(run.completed_at for run in runs) >= rollback[0].observed_at or any(
        a.observed_at >= b.observed_at for a, b in pairwise(rollback)
    ):
        gaps.append(
            "rollback must finish the lab runs, uninstall, stop, reset-failed and read inactive before Mac reinstall"
        )
    return gaps
