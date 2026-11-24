# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly sender on the runtime's tick.

``handle`` takes the runtime tick. A tick inside the overlay's interval, a night already settled in the sent record and an
overlay that is not there yet return nothing. Otherwise the tick looks for the nightly run of the slot through the repository
gateway: a run that is not terminal (or has not settled) is a run event that says waiting; a terminal run gets the collect
command for the slot date, and only a collected answer gets the publish command for the same date. The night is then marked
settled in the sent record, which a restart reads again. A night that cannot be sent within ``collector_max_lag_hours`` of its
slot is a failed run event that names the date and the reason (a missed night), recorded once; the dead-man alarm for it stays
with the collect effect's slot check. An unwritable sent record fails the run before any command goes out.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.adapter_bus_command_sender import (
    BusNightlyCommandSender,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.adapter_gateway_nightly_runs import (
    GatewayNightlyRunReader,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.adapter_record_settings import (
    SelectionPolicyRecordSettings,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.sender_slots import (
    missed_after,
    run_for_slot,
    slot_date_for,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.sender_state_file import (
    FileSenderStateStore,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    EnumCollectStatus,
    EnumPublishStatus,
    EnumSlotOutcome,
    ModelCollectCommand,
    ModelPublishCommand,
    ModelSlotRecord,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender_schedule_overlay import (
    ModelNightlySenderScheduleOverlay,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyCommandError,
    NightlyRunReadError,
    ProtocolNightlyCommandSender,
    ProtocolNightlyRunReader,
    ProtocolRecordSettingsReader,
    ProtocolSenderStateStore,
    SenderStateError,
)

NODE_NAME = "node_test_selector_nightly_sender_scheduled_effect"
_SENT_PUBLISH = frozenset(
    {EnumPublishStatus.PUBLISHED, EnumPublishStatus.ALREADY_PUBLISHED}
)


class HandlerNightlySenderScheduled:
    """Gate on the schedule, send the collect and publish commands for a finished nightly, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        runs: ProtocolNightlyRunReader | None = None,
        sender: ProtocolNightlyCommandSender | None = None,
        settings: ProtocolRecordSettingsReader | None = None,
        state: ProtocolSenderStateStore | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelNightlySenderScheduleOverlay, environ=environ
        )
        self._environ = environ
        self._clock = clock
        self._runs = runs
        self._sender = sender
        self._settings = settings
        self._state = state
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def _mark_missed(
        self, state: ProtocolSenderStateStore, slot: date, now: datetime, reason: str
    ) -> str:
        detail = f"missed night slot={slot.isoformat()}: {reason}"
        try:
            state.record(
                ModelSlotRecord(
                    slot_date=slot,
                    outcome=EnumSlotOutcome.MISSED,
                    recorded_at=now,
                    detail=reason,
                )
            )
        except SenderStateError as exc:
            detail += f"; {exc}"
        return detail

    def _unresolved(
        self,
        request: ModelRuntimeTick,
        overlay: ModelNightlySenderScheduleOverlay,
        state: ProtocolSenderStateStore,
        slot: date,
        reason: str,
        *,
        waiting: bool = False,
    ) -> ModelScheduleRunEvent:
        """Past the lag the night is a recorded missed night; before it the next interval tries again."""
        now = request.now.astimezone(UTC)
        if now >= missed_after(slot, overlay):
            return self._event(
                request, "failed", self._mark_missed(state, slot, now, reason)
            )
        if waiting:
            return self._event(
                request, "completed", f"slot={slot.isoformat()} waiting: {reason}"
            )
        return self._event(
            request,
            "failed",
            f"slot={slot.isoformat()} not sent: {reason}; the next interval tries again",
        )

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelNightlySenderScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a nightly sender schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        slot = slot_date_for(now, overlay)
        try:
            state = self._state or FileSenderStateStore(
                state_dir_env=overlay.state_dir_env,
                environ=os.environ if self._environ is None else self._environ,
            )
            if state.settled(slot) is not None:
                return None
            state.ensure_writable()
        except SenderStateError as exc:
            return self._event(
                request, "failed", f"slot={slot.isoformat()} not sent: {exc}"
            )
        return self._send(request, overlay, state, slot)

    def _send(
        self,
        request: ModelRuntimeTick,
        overlay: ModelNightlySenderScheduleOverlay,
        state: ProtocolSenderStateStore,
        slot: date,
    ) -> ModelScheduleRunEvent:
        now = request.now.astimezone(UTC)
        reader = self._runs or GatewayNightlyRunReader()
        try:
            listed = reader.list_runs(
                repository=overlay.repository, workflow_file=overlay.workflow_file
            )
        except NightlyRunReadError as exc:
            return self._unresolved(
                request, overlay, state, slot, f"the nightly runs were not read: {exc}"
            )
        run = run_for_slot(listed, slot, overlay)
        if run is None:
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                "no nightly run for the slot yet",
                waiting=True,
            )
        if not run.terminal:
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"nightly run {run.run_id} is {run.status}",
                waiting=True,
            )
        if now < run.updated_at + timedelta(seconds=overlay.settle_margin_seconds):
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"nightly run {run.run_id} finished inside the settle margin",
                waiting=True,
            )
        try:
            values = (self._settings or SelectionPolicyRecordSettings()).read()
        except ValueError as exc:
            return self._unresolved(request, overlay, state, slot, str(exc))
        sender = self._sender or BusNightlyCommandSender(
            bus_lane=overlay.bus_lane,
            timeout_s=overlay.command_timeout_seconds,
            workspace_config_root=overlay.workspace_config_root,
        )
        try:
            collected = sender.collect_nightly(
                ModelCollectCommand(slot_date=slot, now=now)
            )
        except NightlyCommandError as exc:
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"collect_nightly was not answered: {exc}",
            )
        if (
            collected.status is not EnumCollectStatus.COLLECTED
            or collected.slot_date != slot
        ):
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"collect_nightly answered {collected.status.value} for {collected.slot_date.isoformat()}"
                + (f" ({collected.failure})" if collected.failure else "")
                + "; publish_attribution_record was not sent",
            )
        try:
            published = sender.publish_attribution_record(
                ModelPublishCommand(
                    slot_date=slot,
                    now=now,
                    collect_result=collected.raw,
                    selector_version=values.selector_version,
                    policy_version=values.policy_version,
                    stage_entry_counted=values.stage_entry_counted,
                )
            )
        except NightlyCommandError as exc:
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"collect_nightly collected; publish_attribution_record was not answered: {exc}",
            )
        if published.status not in _SENT_PUBLISH:
            return self._unresolved(
                request,
                overlay,
                state,
                slot,
                f"collect_nightly collected; publish_attribution_record answered {published.status.value}"
                + (f" ({published.failure})" if published.failure else ""),
            )
        detail = (
            f"slot={slot.isoformat()} sent collect_nightly=collected "
            f"publish_attribution_record={published.status.value}"
        )
        try:
            state.record(
                ModelSlotRecord(
                    slot_date=slot,
                    outcome=EnumSlotOutcome.SENT,
                    recorded_at=now,
                    detail=detail,
                )
            )
        except SenderStateError as exc:
            return self._event(
                request, "failed", f"{detail}; the sent record was not written: {exc}"
            )
        return self._event(request, "completed", detail)


__all__: list[str] = ["HandlerNightlySenderScheduled"]
