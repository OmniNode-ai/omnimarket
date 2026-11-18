# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bind the Temporal ready-edge activity to the dev runtime and lab alarm delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
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
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import load_overlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler as build_delivery_handler,
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


def read_dev_runtime(
    settings: ModelRunnerReadReadySettings, environ: Mapping[str, str]
) -> ModelRuntimeReadySnapshot:
    """Read the runtime's own /ready and build provenance within one container boot."""
    host_reads = load_structured_overlay(
        settings.host_reads_overlay_id,
        ModelRuntimeHostReadOverlay,
        environ,
        file_env=settings.host_reads_file_env,
    )
    host = environ.get(host_reads.ssh_host_env, "").strip()
    if not host:
        raise ValueError(f"{host_reads.ssh_host_env} is not set")

    script = "\n".join(
        [
            "set -eu",
            "RT=$(docker ps --filter "
            f"label=com.docker.compose.project={host_reads.compose_project} --filter "
            f"label=com.docker.compose.service={host_reads.provenance_service} "
            "--filter status=running --no-trunc --format '{{.ID}}')",
            'test -n "$RT"',
            # Docker validates that the same, still-running container supplied both observations.
            """snapshot() { docker inspect --format '{{.State.Running}} {{.State.StartedAt}}' "$RT"; }""",
            'before="$(snapshot)"',
            'case "$before" in "true "*) ;; *) exit 4 ;; esac',
            f"code=$(curl --silent --show-error --max-time {settings.probe_timeout_seconds} "
            f"--output /dev/null --write-out '%{{http_code}}' {shlex.quote(settings.ready_url)})",
            """printf '%s\\n%s\\n' "${before#true }" "$code" """,
            f'docker exec "$RT" cat {host_reads.provenance_path}',
            'test "$(snapshot)" = "$before"',
        ]
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host, "bash -s"],
        input=script,
        text=True,
        capture_output=True,
        timeout=host_reads.timeout_seconds,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"dev runtime probe exited {result.returncode}")
    started_at, status, raw_build = result.stdout.split("\n", 2)
    if status not in {"200", "503"}:
        raise ValueError("dev /ready returned an unexpected status")
    # Old or incomplete provenance is unread; using the container start as the
    # build time would exclude merges that landed between that build and boot.
    build = json.loads(raw_build)
    if not isinstance(build, dict) or not isinstance(build.get("build_time"), str):
        raise ValueError("the dev runtime's provenance has no build time")
    return ModelRuntimeReadySnapshot(
        started_at=datetime.fromisoformat(started_at),
        built_at=datetime.fromisoformat(build["build_time"]),
        ready=status == "200",
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
