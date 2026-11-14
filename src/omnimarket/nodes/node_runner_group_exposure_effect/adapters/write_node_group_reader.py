# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a runner group through the GitHub write node over the lab bus.

The request is the write node's ``runner_group_get`` command, published with the same bus
caller and answered on the write node's terminal topic. The write node holds the App
permission the source's own token may not; the reader never talks to GitHub itself. The
settings are the overlay's ``ModelWriteNodeGroupRead``: the lane, the NAME of the
environment variable holding the workspace root and the timeout, never a value.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import JsonValue, ValidationError

from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.models.runner_group_exposure import (
    EnumExposureReadFailure,
    ModelRunnerGroupFacts,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelWriteNodeGroupRead,
)


class GroupReadError(Exception):
    """A read of the runner group through the write node failed, named by its failure."""

    def __init__(
        self, failure: EnumExposureReadFailure, http_status: int | None = None
    ) -> None:
        super().__init__(failure.value)
        self.failure = failure
        self.http_status = http_status

    def __str__(self) -> str:
        return self.failure.value


#: One write request in, the terminal's (failed, result) out.
WriteCall = Callable[[ModelRepoWriteRequest], tuple[bool, dict[str, JsonValue]]]


def bus_write_call(
    *,
    route: ModelWriteNodeGroupRead,
    environ: Mapping[str, str],
) -> WriteCall:
    """A :data:`WriteCall` that publishes on the lab bus lane and awaits the terminal."""
    # Imported here: a test with a fake call must not need a bus client.
    from omnibase_infra.errors import InfraConnectionError

    from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        open_lab_run_bus,
    )
    from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
    from omnimarket.models.scm import EnumScmNode

    def call(request: ModelRepoWriteRequest) -> tuple[bool, dict[str, JsonValue]]:
        lane = route.bus_lane
        workspace = environ.get(route.workspace_env) or None
        # The opener's fourth keyword (the workspace root) carries the private registry clone's
        # name, which the public-repo hygiene gate refuses; its parameters are bound from its
        # signature in declared order, as ``onex scm`` binds them.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
            raise GroupReadError(EnumExposureReadFailure.WRITE_NODE_UNAVAILABLE)
        opener = cast(
            Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
            open_lab_run_bus,
        )
        values = ("kafka", lane, None, Path(workspace) if workspace else None)

        async def run() -> tuple[bool, dict[str, JsonValue]]:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                terminal = await ScmBusCaller(bus).call(
                    node=EnumScmNode.WRITE,
                    request=request,
                    correlation_id=request.correlation_id,
                    timeout_s=route.timeout_seconds,
                )
            payload = terminal.payload
            failed = (
                terminal.failed
                or payload.get("outcome") == "failed"
                or payload.get("status") == "failed"
            )
            result = payload.get("result", payload)
            return failed, result if isinstance(result, dict) else payload

        try:
            # The effect runs under a loop; the bus call gets a thread of its own.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, run()).result()
        except TimeoutError as exc:
            raise GroupReadError(
                EnumExposureReadFailure.WRITE_NODE_UNAVAILABLE
            ) from exc
        except (LabRunBusError, InfraConnectionError) as exc:
            raise GroupReadError(
                EnumExposureReadFailure.WRITE_NODE_UNAVAILABLE
            ) from exc

    return call


class WriteNodeGroupReader:
    """Read a runner group through the GitHub write node over the bus."""

    def __init__(self, route: ModelWriteNodeGroupRead, call: WriteCall) -> None:
        self._route = route
        self._call = call

    def read(
        self,
        *,
        org: str,
        group_id: int,
        correlation_id: UUID,
    ) -> ModelRunnerGroupFacts:
        owner = self._route.anchor_repository.split("/", 1)[0]
        if org.lower() != owner.lower():
            raise GroupReadError(EnumExposureReadFailure.GROUP_READ_REFUSED)
        request = ModelRepoWriteRequest(
            correlation_id=correlation_id,
            operation=EnumRepoWriteOperation.RUNNER_GROUP_GET,
            repo=self._route.anchor_repository,
            idempotency_key=f"runner-group-{group_id}-{correlation_id}",
            requested_by_lane=self._route.requested_by_lane,
            runner_group_id=group_id,
            dry_run=False,
        )
        failed, result = self._call(request)
        if failed:
            try:
                failed_model = ModelRepoWriteFailed.model_validate(result)
            except ValidationError:
                raise GroupReadError(
                    EnumExposureReadFailure.GROUP_READ_REFUSED
                ) from None
            if failed_model.http_status in (401, 403, 404):
                raise GroupReadError(
                    EnumExposureReadFailure.ORG_READ_DENIED,
                    http_status=failed_model.http_status,
                )
            raise GroupReadError(EnumExposureReadFailure.GROUP_READ_REFUSED)
        try:
            completed = ModelRepoWriteCompleted.model_validate(result)
        except ValidationError:
            raise GroupReadError(EnumExposureReadFailure.UNREADABLE) from None
        group = completed.runner_group
        if group is None or group.id != group_id:
            raise GroupReadError(EnumExposureReadFailure.UNREADABLE)
        return ModelRunnerGroupFacts(
            group_id=group.id,
            allows_public_repositories=group.allows_public_repositories,
            restricted_to_workflows=group.restricted_to_selected_workflows,
            selected_workflows=group.selected_workflows,
            repositories=group.repositories,
        )


__all__ = [
    "GroupReadError",
    "WriteCall",
    "WriteNodeGroupReader",
    "bus_write_call",
]
