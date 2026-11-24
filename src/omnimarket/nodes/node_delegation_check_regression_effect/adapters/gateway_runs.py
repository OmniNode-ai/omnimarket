# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check's finished workflow runs, read through the SCM gateway node (the lane's own ``gh`` is refused for reads)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from omnimarket.handlers.scm.handler_gateway_read_client import (
    GatewayReadError,
    HandlerGatewayReadClient,
    _token_resolves,
    read_in_process,
    read_over_bus,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumRunVerdict,
    ModelProbeRun,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProbeRunsUnreadableError,
)

_PASS = frozenset({"success"})
_FAIL = frozenset({"failure", "timed_out", "startup_failure"})


class ProtocolGatewayReader(Protocol):
    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult: ...


def workflow_basename(workflow: str) -> str:
    """The gateway takes the workflow file's name, not its path."""
    return workflow.rsplit("/", 1)[-1]


class GatewayProbeRunSource:
    """One gateway read per check and admitted event; a run that is neither a success nor a failure is not a result."""

    def __init__(
        self, timeout_seconds: float, client: ProtocolGatewayReader | None = None
    ) -> None:
        self._timeout = timeout_seconds
        self._client = client or HandlerGatewayReadClient(route=self._route)

    def _route(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        if _token_resolves():
            return read_in_process(request)
        return read_over_bus(request, timeout_s=self._timeout)

    def runs(self, check: ModelWatchedCheck, limit: int) -> tuple[ModelProbeRun, ...]:
        found: list[ModelProbeRun] = []
        for event in check.events:
            request = ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
                repo=check.repository,
                workflow=workflow_basename(check.workflow),
                event=event,
                status="completed",
                limit=limit,
            )
            try:
                result = self._client.read(request)
            except GatewayReadError as exc:
                raise ProbeRunsUnreadableError(str(exc)) from exc
            if not isinstance(result, ModelWorkflowRunsResult):
                raise ProbeRunsUnreadableError(
                    f"the gateway answered {type(result).__name__}, not workflow runs"
                )
            for run in result.runs:
                verdict = (
                    EnumRunVerdict.PASS
                    if run.conclusion in _PASS
                    else EnumRunVerdict.FAIL
                    if run.conclusion in _FAIL
                    else None
                )
                if verdict is None:
                    continue
                try:
                    finished = datetime.fromisoformat(run.updated_at or run.created_at)
                except ValueError as exc:
                    raise ProbeRunsUnreadableError(
                        f"run {run.id} carries an unreadable time"
                    ) from exc
                found.append(
                    ModelProbeRun(
                        run_id=str(run.id),
                        at=finished
                        if finished.tzinfo is not None
                        else finished.replace(tzinfo=UTC),
                        verdict=verdict,
                        ref=run.html_url,
                    )
                )
        return tuple(found)
