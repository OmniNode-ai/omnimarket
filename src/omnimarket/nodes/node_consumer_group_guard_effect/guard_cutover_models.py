# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Portable, timestamped readbacks of the guard's ordered host cutover.

The caller captures these values from the Mac's installer and launchctl, and
from the lab's installer, journal, census.json and LastTriggerUSec. Validation
checks their consistency; it does not collect or authenticate host readbacks.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictInt, StringConstraints

from .guard_schedule_models import NonEmptyStr, _Frozen

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
InvocationId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ModelGuardReadback(_Frozen):
    """Completion time, exit code and stdout captured from one host command."""

    observed_at: AwareDatetime
    exit_code: StrictInt
    stdout: str


class ModelGuardImplementationDigests(_Frozen):
    """SHA256 of the same implementation bytes read from the three installs."""

    mac_clone: Digest
    lab_clone: Digest
    lab_dispatch: Digest


class ModelGuardBusIdentity(_Frozen):
    """Broker and bus lane read from the guard's census."""

    broker: NonEmptyStr
    bus_lane: NonEmptyStr


class ModelGuardTimerRun(ModelGuardBusIdentity):
    """One completed journal invocation, its timer trigger and its census."""

    invocation_id: InvocationId
    trigger: Literal["timer"]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    last_trigger_at: AwareDatetime
    observed_at: AwareDatetime
    status: Literal["OK"]
    reap_detail: NonEmptyStr


class ModelGuardCutoverEvidence(_Frozen):
    """Readbacks required before the guard's cutover can be declared complete.

    mac_absent_* records are `launchctl list ai.omninode.consumer-group-guard`.
    rollback_* records are schedule uninstall, systemctl stop, reset-failed and
    is-active of the lab service, followed by the Mac installer readback.
    """

    host: NonEmptyStr
    implementation_digests: ModelGuardImplementationDigests
    mac_unload: ModelGuardReadback
    mac_absent_after_unload: ModelGuardReadback
    lab_install: ModelGuardReadback
    host_reconcile: ModelGuardReadback
    mac_absent_after_reconcile: ModelGuardReadback
    other_unit_name: NonEmptyStr
    other_unit_install: ModelGuardReadback
    mac_absent_after_other_install: ModelGuardReadback
    mac_baseline: ModelGuardBusIdentity
    runs: Annotated[tuple[ModelGuardTimerRun, ...], Field(min_length=2)]
    rollback_uninstall: ModelGuardReadback
    rollback_stop: ModelGuardReadback
    rollback_reset_failed: ModelGuardReadback
    rollback_is_active: ModelGuardReadback
    mac_reinstall: ModelGuardReadback
