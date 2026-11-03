# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wait for the projection row of one pull request to reach a terminal verdict."""

from collections.abc import Callable
from datetime import datetime

from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionRefusal,
)
from omnimarket.models.pr_state.model_pr_state_read_request import (
    ModelPrStateReadRequest,
    ModelPrStateWaitRequest,
)
from omnimarket.models.pr_state.model_pr_state_read_result import (
    ModelPrStateReadFresh,
    ModelPrStateReadResult,
    ModelPrStateReadStale,
    ModelPrStateReadTimeout,
    ModelPrStateReadUnavailable,
)
from omnimarket.models.pr_state.model_pr_state_row import ModelPrStateRow
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_read import (
    HandlerPrStateRead,
)
from omnimarket.nodes.node_pr_state_read_effect.ports.protocol_pr_state_page_source import (
    ProtocolPrStatePageSource,
)


class HandlerPrStateWait:
    """Reads the projection, then re-reads only rows observed after the last page.

    A row counts only when it is fresh, matches the requested head when one is
    given, and has a terminal verdict or a terminal pull request state.
    """

    def __init__(
        self,
        source: ProtocolPrStatePageSource,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self._source = source
        self._clock = clock
        self._sleep = sleep
        self._read = HandlerPrStateRead()

    def handle(self, request: ModelPrStateWaitRequest) -> ModelPrStateReadResult:
        policy = request.policy
        started = self._clock()
        cursor: str | None = None
        last_fresh: ModelPrStateRow | None = None
        last_stale_at: datetime | None = None
        while True:
            page = self._source.fetch(cursor)
            if isinstance(page, ModelPrStateProjectionRefusal):
                return ModelPrStateReadResult(
                    result=ModelPrStateReadUnavailable(
                        status_code=page.status_code, error=page.error
                    )
                )
            now = self._clock()
            judged = self._read.handle(
                ModelPrStateReadRequest(
                    repo=request.repo,
                    pr_number=request.pr_number,
                    head_sha=request.head_sha,
                    page=page,
                    now=now,
                    policy=policy,
                )
            ).result
            if isinstance(judged, ModelPrStateReadFresh):
                last_fresh = judged.row
                if (
                    judged.row.required_verdict in policy.terminal_verdicts
                    or judged.row.state in policy.terminal_states
                ):
                    return ModelPrStateReadResult(result=judged)
            elif isinstance(judged, ModelPrStateReadStale):
                last_stale_at = judged.observed_at
            if page.cursor is not None:
                cursor = page.cursor
            waited = (self._clock() - started).total_seconds()
            if waited >= request.timeout_seconds:
                return ModelPrStateReadResult(
                    result=ModelPrStateReadTimeout(
                        repo=request.repo,
                        pr_number=request.pr_number,
                        head_sha=request.head_sha,
                        waited_seconds=waited,
                        timeout_seconds=request.timeout_seconds,
                        last_observed_at=(
                            last_fresh.observed_at
                            if last_fresh is not None
                            else last_stale_at
                        ),
                        last_head_sha=(
                            last_fresh.head_sha if last_fresh is not None else None
                        ),
                    )
                )
            self._sleep(min(policy.wait_poll_seconds, request.timeout_seconds - waited))
