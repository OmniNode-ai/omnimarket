# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open pull requests read from the GitHub gateway over the bus, for the duplicate-fix guard (OMN-22654).

A lab host has no PR watcher state, so ``onex scm pr-create`` there could not run the duplicate-fix guard and
refused every pull request a cause or fix worker tried to open. This source gives the guard a live read on such a
host: one ``open_pr_facts`` request to the gateway, answered with every open pull request of the repository.

The read is complete or it is an error. A bus that cannot be opened, a gateway that does not answer or does not
serve the operation, a failed answer and an answer that is not the typed result each raise
``OpenPrFactsUnreadableError`` naming the read that failed; the guard then stays unchecked and opens nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Final

from omnibase_infra.errors import InfraConnectionError
from pydantic import BaseModel, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.model_github_gateway_io import (
    MAX_LIST_LIMIT,
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
    ModelMergedPrResult,
    ModelOpenPrFactsResult,
)
from omnimarket.models.scm import EnumScmNode, ModelScmTerminal
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.ports.protocol_open_pr_facts_source import (
    OpenPrFactsUnreadableError,
)

#: A read the gateway answers in seconds; a guard that waits longer than this has no answer to give.
DEFAULT_TIMEOUT_S: Final[float] = 60.0
DEFAULT_ATTEMPTS: Final[int] = 2
RETRY_PAUSE_S: Final[float] = 5.0

OpenBus = Callable[
    [dict[str, object]], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
]


class GatewayOpenPrFactsSource:
    """``ProtocolOpenPrFactsSource`` over the source-control gateway's ``open_pr_facts`` operation."""

    def __init__(
        self,
        *,
        open_bus: OpenBus,
        bus_opts: dict[str, object],
        timeout_s: float = DEFAULT_TIMEOUT_S,
        attempts: int = DEFAULT_ATTEMPTS,
        pause_s: float = RETRY_PAUSE_S,
    ) -> None:
        self._open_bus = open_bus
        self._bus_opts = bus_opts
        self._timeout_s = timeout_s
        self._attempts = max(1, attempts)
        self._pause_s = pause_s

    def read(self, repo: str) -> ModelOpenPrFactsResult:
        return self._read(
            repo,
            lambda correlation_id: ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.OPEN_PR_FACTS,
                repo=repo,
                limit=MAX_LIST_LIMIT,
                correlation_id=correlation_id,
            ),
            ModelOpenPrFactsResult,
            "open_pr_facts",
        )

    def read_pr(self, repo: str, number: int) -> ModelMergedPrResult:
        """One pull request in any state: the read behind a ``replaces`` entry the watcher state no longer holds (OMN-23634)."""
        return self._read(
            repo,
            lambda correlation_id: ModelGithubGatewayRequest(
                operation=EnumGithubGatewayOperation.MERGED_PR,
                repo=repo,
                pr_number=number,
                correlation_id=correlation_id,
            ),
            ModelMergedPrResult,
            "merged_pr",
        )

    def _read[Result: BaseModel](
        self,
        repo: str,
        build: Callable[[uuid.UUID], ModelGithubGatewayRequest],
        result_type: type[Result],
        operation: str,
    ) -> Result:
        last = "no attempt was made"
        for attempt in range(1, self._attempts + 1):
            try:
                return self._read_once(repo, build, result_type, operation)
            except _RetryableOpenPrFactsError as exc:
                last = str(exc)
                if attempt < self._attempts:
                    time.sleep(self._pause_s)
        raise OpenPrFactsUnreadableError(last)

    def _read_once[Result: BaseModel](
        self,
        repo: str,
        build: Callable[[uuid.UUID], ModelGithubGatewayRequest],
        result_type: type[Result],
        operation: str,
    ) -> Result:
        correlation_id = uuid.uuid4()
        try:
            request = build(correlation_id)
        except ValidationError as exc:
            raise OpenPrFactsUnreadableError(
                f"{repo!r} is not an OWNER/REPO the gateway can read: {exc.error_count()} validation error(s)"
            ) from None
        try:
            terminal = asyncio.run(self._call(request, correlation_id))
        except TimeoutError:
            raise _RetryableOpenPrFactsError(
                f"the gateway gave no answer within {self._timeout_s:g}s"
            ) from None
        except (LabRunBusError, InfraConnectionError) as exc:
            # No retry: a bus the lane cannot open or may not use is the same on the next attempt, and the guard
            # must answer quickly (the answer is "unchecked", naming it).
            raise OpenPrFactsUnreadableError(
                f"the bus could not be used: {exc}"
            ) from None
        return _typed_result(terminal, result_type, operation)

    async def _call(
        self, request: ModelGithubGatewayRequest, correlation_id: uuid.UUID
    ) -> ModelScmTerminal:
        async with self._open_bus(
            {**self._bus_opts, "scm_node": EnumScmNode.GATEWAY}
        ) as bus:
            return await ScmBusCaller(bus).call(
                node=EnumScmNode.GATEWAY,
                request=request,
                correlation_id=correlation_id,
                timeout_s=self._timeout_s,
            )


class _RetryableOpenPrFactsError(OpenPrFactsUnreadableError):
    """A failure a second attempt may not repeat: the gateway gave no answer in time."""


def _typed_result[Result: BaseModel](
    terminal: ModelScmTerminal, result_type: type[Result], operation: str
) -> Result:
    payload = terminal.payload
    if (
        terminal.failed
        or payload.get("outcome") == "failed"
        or payload.get("status") == "failed"
    ):
        reason = (
            payload.get("reason")
            or payload.get("error")
            or payload.get("detail")
            or f"its terminal event carried {json.dumps(payload, sort_keys=True)[:300]}"
        )
        raise OpenPrFactsUnreadableError(f"the gateway refused it: {reason}")
    result = payload.get("result", payload)
    try:
        return result_type.model_validate(result)
    except ValidationError as exc:
        raise OpenPrFactsUnreadableError(
            f"the gateway's answer is not the {operation} result "
            f"({exc.error_count()} validation error(s); a gateway older than this client does not serve the operation)"
        ) from None


__all__: list[str] = ["GatewayOpenPrFactsSource"]
