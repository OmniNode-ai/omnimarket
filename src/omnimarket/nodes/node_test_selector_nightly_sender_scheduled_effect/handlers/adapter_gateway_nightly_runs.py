# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly's run list, read through the repository gateway node."""

from __future__ import annotations

from pydantic import ValidationError

from omnimarket.handlers.scm.handler_gateway_read_client import (
    GatewayReadError,
    HandlerGatewayReadClient,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelWorkflowRunsResult,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    ModelNightlyRunView,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyRunReadError,
)

#: The trigger of the nightly run the stage record counts.
SCHEDULED_EVENT = "schedule"


class GatewayNightlyRunReader:
    """Lists the workflow's scheduled runs with the gateway read client the other readers share."""

    def __init__(self, client: HandlerGatewayReadClient | None = None) -> None:
        self._client = client or HandlerGatewayReadClient()

    def list_runs(
        self, *, repository: str, workflow_file: str
    ) -> tuple[ModelNightlyRunView, ...]:
        request = ModelGithubGatewayRequest(
            operation=EnumGithubGatewayOperation.WORKFLOW_RUNS,
            repo=repository,
            workflow=workflow_file,
            event=SCHEDULED_EVENT,
        )
        try:
            result = self._client.read(request)
        except GatewayReadError as exc:
            raise NightlyRunReadError(f"gateway workflow_runs: {exc}") from exc
        if not isinstance(result, ModelWorkflowRunsResult):
            raise NightlyRunReadError(
                f"gateway workflow_runs answered {type(result).__name__}"
            )
        try:
            return tuple(
                ModelNightlyRunView(
                    run_id=run.id,
                    status=run.status,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
                for run in result.runs
            )
        except ValidationError as exc:
            raise NightlyRunReadError(
                "gateway workflow_runs listed a run with an unreadable timestamp"
            ) from exc


__all__: list[str] = ["GatewayNightlyRunReader"]
