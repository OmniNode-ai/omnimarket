# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabFillLauncher: wires the fire's facts into the pure compute judge.

Reads the fire's result file, run log and snapshot via the injected file port,
calls the compute node's handler in two passes (first to learn the alert, then
delivers it and judges again), and publishes the validated result.
"""

from __future__ import annotations

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers.handler_lab_fill_fire_outcome import (
    HandlerLabFillFireOutcome,
    first_refused_line,
    first_word_marker_seen,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models.model_lab_fill_fire_outcome import (
    ModelLabFillFireOutcomeRequest,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.models.model_lab_fill_launcher import (
    ModelLabFillLauncherRequest,
    ModelLabFillLauncherResult,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.overlay import (
    ModelLabFillLauncherOverlay,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.protocols.protocol_lab_fill_launcher import (
    ProtocolLabFillAlertSender,
    ProtocolLabFillClock,
    ProtocolLabFillFireFiles,
)


class HandlerLabFillLauncher:
    """Builds the compute request from the fire's files and orchestrates the two-pass judge."""

    def __init__(
        self,
        overlay: ModelLabFillLauncherOverlay,
        files: ProtocolLabFillFireFiles,
        alerts: ProtocolLabFillAlertSender,
        clock: ProtocolLabFillClock,
    ) -> None:
        self._overlay = overlay
        self._files = files
        self._alerts = alerts
        self._clock = clock
        self._judge = HandlerLabFillFireOutcome()

    def handle(
        self, request: ModelLabFillLauncherRequest
    ) -> ModelLabFillLauncherResult:
        """Judge one fire: read its files, deliver its alert, publish its result."""
        policy = self._overlay.policy
        exists, nonempty, text = self._files.read_result(request.fire_id)
        log_text = self._files.read_log(request.run_log)
        truncated_marker = (
            first_word_marker_seen(log_text, policy.truncated_marker)
            if request.result_channel_enabled
            else False
        )
        refused_line = first_refused_line(log_text, policy)
        snapshot_name = self._overlay.state.snapshot_name_template.format(
            date=self._clock.today_utc().strftime(self._overlay.state.date_format)
        )
        snapshot_present = self._files.snapshot_present(snapshot_name)
        latest_path = self._files.latest_path()

        compute_request = ModelLabFillFireOutcomeRequest(
            policy=policy,
            fire_id=request.fire_id,
            phase=request.phase,
            exit=request.exit,
            selftest=request.selftest,
            result_channel_enabled=request.result_channel_enabled,
            workflow_result_file=request.workflow_result_file,
            truncated_marker_seen=truncated_marker,
            file_state="present" if exists else "absent",
            file_nonempty=nonempty,
            file_text=text,
            run_log_refused_line=refused_line,
            run_log_path=request.run_log,
            latest_path=latest_path,
            snapshot_name=snapshot_name,
            snapshot_present=snapshot_present,
            alert_exit=None,
        )

        first_result = self._judge.handle(compute_request)

        alert_exit: int | None = None
        if first_result.alert is not None:
            alert_exit = self._alerts.send(
                first_result.alert.category.value, first_result.alert.text
            )
            compute_request = compute_request.model_copy(
                update={"alert_exit": alert_exit}
            )
            final_result = self._judge.handle(compute_request)
        else:
            final_result = first_result

        if final_result.latest_value is not None:
            self._files.write_latest(final_result.latest_value)

        return ModelLabFillLauncherResult(
            phase=final_result.phase,
            exit=final_result.exit,
            degraded_class=final_result.degraded_class,
            result_channel=final_result.result_channel.value
            if final_result.result_channel
            else None,
            result_truncated=final_result.result_truncated,
            notice_line=final_result.notice_line,
            alert_exit=final_result.alert_exit,
            alert_category=final_result.alert.category.value
            if final_result.alert
            else None,
        )
