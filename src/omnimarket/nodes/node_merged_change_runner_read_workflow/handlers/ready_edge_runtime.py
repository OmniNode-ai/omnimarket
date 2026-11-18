# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the Temporal ready-edge activity to the dev runtime and lab alarm delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    RuntimeProbeSource,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import ModelProbeSettings
from omnimarket.nodes.node_lab_alarm.overlay import (
    load_overlay as load_lab_alarm_overlay,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import load_overlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler as build_delivery_handler,
)
from omnimarket.nodes.node_merged_change_reading_effect.runtime_merged_change_reading import (
    load_log_labels,
)
from omnimarket.nodes.node_merged_change_reading_effect.sources.source_runtime_log import (
    SourceRuntimeFacts,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.handler_runner_read_ready_edge import (
    HandlerRunnerReadReadyEdge,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_ready_edge import (
    READY_ACTIVITY_NAME,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
    WORKFLOW_NAME,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
    ModelRunnerReadPlan,
)
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read_ready_edge import (
    ModelRunnerReadReadyEdge,
    ModelRunnerReadReadyRequest,
    ModelRunnerReadReadySettings,
    ModelRunnerReadReadyState,
    ModelRuntimeReadySnapshot,
)

logger = logging.getLogger(__name__)


def _ready_status(url: str, timeout_seconds: int) -> int:
    """The HTTP status of the runtime's own readiness answer; a 503 is an answer, not a failed read."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("the declared ready URL is not an http URL")
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            status: int = response.status
            return status
    except urllib.error.HTTPError as exc:
        return exc.code


def _ready_url(environ: Mapping[str, str]) -> str:
    """The runtime's readiness URL, from the lab alarm overlay that already declares it."""
    receipt = load_lab_alarm_overlay(environ).lab_pass_receipt
    if receipt is None or not receipt.ready_url:
        raise ValueError("the lab alarm overlay declares no runtime ready URL")
    return receipt.ready_url


def read_dev_runtime(
    settings: ModelRunnerReadReadySettings, environ: Mapping[str, str]
) -> ModelRuntimeReadySnapshot:
    """Read the runtime's own /ready and its build provenance within one container boot.

    The runtime host reads go through the effect nodes that own them: the reading effect's runtime
    facts source reads the container start, the dev deploy reading effect's probe source reads the
    build provenance, and the readiness URL comes from the declared lab alarm overlay.
    """
    host_reads = load_structured_overlay(
        settings.host_reads_overlay_id,
        ModelRuntimeHostReadOverlay,
        environ,
        file_env=settings.host_reads_file_env,
    )
    facts = SourceRuntimeFacts(
        overlay=host_reads,
        labels=load_log_labels(),
        unit_hosts=(),
        environ=environ,
    )
    ready_url = _ready_url(environ)
    provenance = RuntimeProbeSource(
        ModelProbeSettings(
            health_url=ready_url,
            health_ok_path=("ready",),
            ssh_host_env=host_reads.ssh_host_env,
            compose_project=host_reads.compose_project,
            provenance_service=host_reads.provenance_service,
            provenance_path=host_reads.provenance_path,
            sha_path=("build_time",),
            timeout_seconds=host_reads.timeout_seconds,
        ),
        environ,
    )
    started_at = facts.build_started_at()
    status = _ready_status(ready_url, settings.probe_timeout_seconds)
    if status not in {200, 503}:
        raise ValueError("dev /ready returned an unexpected status")
    build = provenance.build_record()
    # The same container must have supplied the start and the provenance; a boot between the
    # reads leaves the sample unread rather than mixing two boots.
    if facts.build_started_at() != started_at:
        raise ValueError("the dev runtime restarted during the probe")
    # Old or incomplete provenance is unread; using the container start as the
    # build time would exclude merges that landed between that build and boot.
    if not isinstance(build.get("build_time"), str):
        raise ValueError("the dev runtime's provenance has no build time")
    return ModelRuntimeReadySnapshot(
        started_at=started_at,
        built_at=datetime.fromisoformat(build["build_time"]),
        ready=status == 200,
    )


class ReadyEdgeActivities:
    """The existing worker executes these operations under the schedule's overlap policy."""

    def __init__(
        self,
        client: Client,
        plan: ModelRunnerReadPlan,
        settings: ModelRunnerReadReadySettings,
        *,
        task_queue: str,
        workflow_id_prefix: str,
        home: Path,
    ) -> None:
        self._client = client
        self._plan = plan
        self._settings = settings
        self._task_queue = task_queue
        self._workflow_id_prefix = workflow_id_prefix
        self._home = home
        self._rpc_timeout = timedelta(seconds=settings.probe_timeout_seconds)
        self._handler = HandlerRunnerReadReadyEdge(
            start=self._start_read,
            completed_at=self._read_completed_at,
            deliver=self._deliver,
            idle_bound=timedelta(seconds=settings.idle_bound_seconds),
        )

    def _read_id(self, edge: ModelRunnerReadReadyEdge) -> str:
        boot = edge.started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{self._workflow_id_prefix}-ready-{boot}"

    async def _start_read(self, edge: ModelRunnerReadReadyEdge) -> None:
        plan = self._plan.model_copy(
            update={"previous_build_at": edge.previous_build_at}
        )
        try:
            await self._client.start_workflow(
                WORKFLOW_NAME,
                plan.model_dump_json(),
                id=self._read_id(edge),
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=self._rpc_timeout,
            )
        except WorkflowAlreadyStartedError:
            # Replaying a partially completed clock tick finds the same read, even if
            # that read finished before the schedule saved that tick's result.
            return

    async def _read_completed_at(
        self, edge: ModelRunnerReadReadyEdge
    ) -> datetime | None:
        execution = await self._client.get_workflow_handle(
            self._read_id(edge)
        ).describe(rpc_timeout=self._rpc_timeout)
        if execution.status is not WorkflowExecutionStatus.COMPLETED:
            return None
        return execution.close_time

    async def _deliver(self, request: ModelLabAlarmDeliveryRequest) -> bool:
        def send() -> bool:
            overlay = load_overlay().model_copy(
                update={
                    "state_path": str(self._home / self._settings.alarm_state_relpath)
                }
            )
            result = build_delivery_handler(overlay).handle(request)
            if result.outcome is EnumDeliveryOutcome.FAILED:
                logger.error("Lab alarm delivery failed for %s", request.condition)
                # The lab alarm persists raised episodes and pending ledger writes
                # even when delivery fails. Keep trying recovery if its post failed.
                if request.outcome is EnumDeliveryCondition.OK:
                    return False
            return result.state_kept

        try:
            return await asyncio.to_thread(send)
        except (OSError, NodeOverlayError) as exc:
            logger.error("Lab alarm is unavailable: %s", type(exc).__name__)
            return False

    @activity.defn(name=READY_ACTIVITY_NAME)
    async def tick(self, state_json: str | None) -> str:
        previous = json.loads(state_json) if state_json is not None else {}
        state = (
            ModelRunnerReadReadyState.model_validate(previous)
            if isinstance(previous, dict) and "state_schema" in previous
            else None
        )
        snapshot = None
        try:
            snapshot = await asyncio.to_thread(
                read_dev_runtime, self._settings, os.environ
            )
        except (
            NodeOverlayError,
            OSError,
            subprocess.SubprocessError,
            ValueError,
        ) as exc:
            logger.warning(
                "Dev runtime readiness sample unread: %s", type(exc).__name__
            )
        result = await self._handler.handle(
            ModelRunnerReadReadyRequest(
                state=state, snapshot=snapshot, now=datetime.now(UTC)
            )
        )
        return result.model_dump_json()
