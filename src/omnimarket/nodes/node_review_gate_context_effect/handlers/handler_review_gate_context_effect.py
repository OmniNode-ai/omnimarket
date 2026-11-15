# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReviewGateContextEffect: post a pull request head's review verdict as the ``fable-review-gate`` commit
status on GitHub through the GitHub write node, idempotently, and read it back.

Canonical shape: ``handle(ModelReviewContextPostCommand) -> ModelReviewContextPostResult``. The reader, poster,
emission sink, emission identity and clock are injected as constructor parameters, so the handler runs from its own
caller and the orchestrator node, not handler_routing.

POST:

* the head's statuses of the decision's context are read; a status of another head or context is never counted;
* the newest status (highest id) whose state equals the decision state is unchanged, and the chain closes success
  with counters {"unchanged": 1} and no post is made;
* otherwise the status is posted through the write node with the decision's head, state, the pull request's link as
  the verdict link and the decision's title as the description (a post failure is a typed failure; no check run is
  ever created), then the head is read back: the highest-id status on the head with that context must have id equal
  to the posted id and state equal to the decision state, else a typed readback failure;
* success closes with counters {"posted": 1}.

Every call is one chain under the floor's emission contract: a start, then a success or a typed failure.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    EnumReviewContextPostOutcome,
    ModelHeadStatus,
    ModelReviewContextPostCommand,
    ModelReviewContextPostResult,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_statuses import (
    ProtocolHeadStatusPoster,
    ProtocolHeadStatusReader,
    ReviewContextPortError,
)


class HandlerReviewGateContextEffect:
    """Posts the review verdict commit status on the exact head, idempotently, and reads it back."""

    def __init__(
        self,
        reader: ProtocolHeadStatusReader,
        poster: ProtocolHeadStatusPoster,
        sink: ProtocolEmissionSink,
        emission_node: str,
        emission_piece: int,
        emission_backend: str,
        route_backend: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._poster = poster
        self._sink = sink
        self._emission_node = emission_node
        self._emission_piece = emission_piece
        self._emission_backend = emission_backend
        self._route_backend = route_backend
        self._clock = clock or (lambda: datetime.now(UTC))

    def _chain(self, request: ModelReviewContextPostCommand) -> ChainEmitter:
        bus = self._route_backend == "bus"
        return ChainEmitter(
            self._sink,
            node=self._emission_node,
            piece=self._emission_piece,
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend="real" if self._emission_backend == "journal" else "in_memory",
            origin="real" if bus else "unknown",
        )

    def handle(
        self, request: ModelReviewContextPostCommand
    ) -> ModelReviewContextPostResult:
        """Post the review verdict commit status on the exact head, idempotently, and read it back."""
        decision = request.decision
        repository = decision.repository
        head_sha = decision.head_sha
        check_name = decision.check_name
        state_value = decision.state.value

        chain = self._chain(request)
        chain.start()

        calls = 0
        try:
            calls += 1
            statuses = self._reader.read(
                repository=repository, head_sha=head_sha, context=check_name
            )
        except ReviewContextPortError as exc:
            chain.failure("review_context_read_failed", str(exc))
            return self._result(
                request,
                EnumReviewContextPostOutcome.FAILED_READ,
                calls,
                detail=str(exc),
            )

        newest = self._newest(statuses, head_sha, check_name)
        if newest is not None and newest.state == state_value:
            chain.success({"unchanged": 1})
            return self._result(request, EnumReviewContextPostOutcome.UNCHANGED, calls)

        try:
            calls += 1
            posted_id = self._poster.post(
                correlation_id=request.correlation_id,
                repository=repository,
                head_sha=head_sha,
                context=check_name,
                state=state_value,
                target_url=f"https://github.com/{repository}/pull/{decision.pr_number}",
                description=decision.title,
            )
        except ReviewContextPortError as exc:
            chain.failure("review_context_post_failed", str(exc))
            return self._result(
                request,
                EnumReviewContextPostOutcome.FAILED_POST,
                calls,
                detail=str(exc),
            )

        try:
            calls += 1
            readback = self._reader.read(
                repository=repository, head_sha=head_sha, context=check_name
            )
        except ReviewContextPortError as exc:
            chain.failure("review_context_readback_mismatch", str(exc))
            return self._result(
                request,
                EnumReviewContextPostOutcome.FAILED_READBACK,
                calls,
                detail=str(exc),
            )

        readback_newest = self._newest(readback, head_sha, check_name)
        if (
            readback_newest is None
            or readback_newest.status_id != posted_id
            or readback_newest.state != state_value
        ):
            chain.failure("review_context_readback_mismatch", "readback mismatch")
            return self._result(
                request,
                EnumReviewContextPostOutcome.FAILED_READBACK,
                calls,
                None,
                "readback mismatch",
            )

        chain.success({"posted": 1})
        return self._result(
            request, EnumReviewContextPostOutcome.POSTED, calls, posted_id, ""
        )

    @staticmethod
    def _newest(
        statuses: tuple[ModelHeadStatus, ...], head_sha: str, context: str
    ) -> ModelHeadStatus | None:
        """The highest-id status on the head with the context; a status of another head or context never counts."""
        matching = tuple(
            s for s in statuses if s.context == context and s.head_sha == head_sha
        )
        if not matching:
            return None
        return max(matching, key=lambda s: s.status_id)

    def _result(
        self,
        request: ModelReviewContextPostCommand,
        outcome: EnumReviewContextPostOutcome,
        calls: int,
        status_id: int | None = None,
        detail: str = "",
    ) -> ModelReviewContextPostResult:
        decision = request.decision
        return ModelReviewContextPostResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            repository=decision.repository,
            pr_number=decision.pr_number,
            head_sha=decision.head_sha,
            check_name=decision.check_name,
            state=decision.state,
            status_id=status_id,
            calls=calls,
            detail=detail,
        )
