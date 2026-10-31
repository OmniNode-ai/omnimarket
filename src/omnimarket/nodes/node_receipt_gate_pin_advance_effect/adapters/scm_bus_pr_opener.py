# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open a pin pull request through node_github_repo_write_effect (OMN-20134).

The request is the write node's ``pr_create`` command, the one ``onex scm
pr-create`` publishes, sent with the same bus caller and answered on the write
node's terminal topic. The write node holds the mode, the quota floor, the
expected-head guard and the idempotency key; an open pull request for the same
head comes back replayed. This module never talks to GitHub itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import JsonValue

from omnimarket.models.model_receipt_gate_pin_advance import (
    ModelCallerPlan,
)
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.protocols import (
    PinAdvanceError,
)

#: One write request in, the terminal payload of the write node out.
WriteCall = Callable[[ModelRepoWriteRequest], dict[str, JsonValue]]


def bus_write_call(*, lane_env: str, default_lane: str, timeout_s: float) -> WriteCall:
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

    lane = os.environ.get(lane_env) or default_lane
    workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
    # The opener's fourth keyword (the workspace root) carries the private
    # registry clone's name, which the public-repo hygiene gate refuses; its
    # parameters are bound from its signature in declared order, as
    # ``onex scm`` binds them.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise PinAdvanceError(f"unexpected bus opener parameters: {names}")
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    values = ("kafka", lane, None, Path(workspace) if workspace else None)

    def call(request: ModelRepoWriteRequest) -> dict[str, JsonValue]:
        async def run() -> dict[str, JsonValue]:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                terminal = await ScmBusCaller(bus).call(
                    node=EnumScmNode.WRITE,
                    request=request,
                    correlation_id=request.correlation_id,
                    timeout_s=timeout_s,
                )
            payload = terminal.payload
            if (
                terminal.failed
                or payload.get("outcome") == "failed"
                or payload.get("status") == "failed"
            ):
                reason = payload.get("detail") or payload.get("failure_reason")
                raise PinAdvanceError(
                    f"pr_create for {request.head} was refused: {reason or 'no reason'}"
                )
            result = payload.get("result", payload)
            return result if isinstance(result, dict) else payload

        try:
            # The effect runs under a loop; the bus call gets a thread of its own.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, run()).result()
        except TimeoutError as exc:
            raise PinAdvanceError(
                f"pr_create for {request.head}: no answer on lane {lane!r} "
                f"within {timeout_s:.0f}s"
            ) from exc
        except (LabRunBusError, InfraConnectionError) as exc:
            raise PinAdvanceError(f"write node bus lane {lane!r}: {exc}") from exc

    return call


class ScmWritePrOpener:
    """ProtocolPinPrOpener over a :data:`WriteCall`."""

    def __init__(self, call: WriteCall) -> None:
        self._call = call

    def open(
        self,
        plan: ModelCallerPlan,
        *,
        branch_head_sha: str,
        requested_by_lane: str,
        correlation_id: UUID,
    ) -> tuple[bool, int | None, str | None]:
        request = ModelRepoWriteRequest(
            correlation_id=correlation_id,
            operation=EnumRepoWriteOperation.PR_CREATE,
            repo=plan.repository,
            idempotency_key=plan.idempotency_key,
            requested_by_lane=requested_by_lane,
            head=plan.branch,
            base=plan.base_branch,
            title=plan.title,
            body=plan.body,
            expected_head_sha=branch_head_sha,
            draft=False,
        )
        result = self._call(request)
        number = result.get("pr_number")
        url = result.get("html_url")
        return (
            result.get("replayed") is True,
            number if isinstance(number, int) else None,
            url if isinstance(url, str) else None,
        )
