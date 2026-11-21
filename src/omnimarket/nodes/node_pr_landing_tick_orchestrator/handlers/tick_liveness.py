# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick's trigger for the liveness read node (OMN-26024).

node_pr_landing_liveness_read_effect has no subscription of its own: nothing sent it a request. The tick
orchestrator, which already runs every 60 s on the floor host in observe mode, now asks it for the head of each
pull request it compares and keeps the answer on its result, so the runtime prints one liveness line per read.

The read is observation only. It runs after the comparison is final, its fact is never put back into the tick's
facts, and a read that raises is recorded as unreadable: no outcome of it can change a decision, a comparison row,
a divergence or a write.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from uuid import uuid4

from pydantic import BaseModel

from omnimarket.models.landing_decision import EnumLivenessRead
from omnimarket.models.pr_landing_liveness_read import (
    ModelLivenessReadRequest,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.handler_pr_landing_liveness_read import (
    HandlerPrLandingLivenessRead,
    build_reader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    load_overlay as load_liveness_overlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    ProtocolTickLivenessReader,
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickLivenessRead,
)


class _BestEffortPublisher:
    """The tick's publisher, whose failure is kept and never raised: a read's event must not end the tick's pass."""

    def __init__(self, inner: ProtocolTickPublisher) -> None:
        self._inner = inner
        self.error = ""

    def publish(self, topic: str, event: BaseModel) -> None:
        try:
            self._inner.publish(topic, event)
        except Exception as exc:
            self.error = type(exc).__name__


class NodeLivenessTickReader:
    """ProtocolTickLivenessReader over the liveness read node's handler."""

    def __init__(
        self,
        handler_factory: Callable[[_BestEffortPublisher], HandlerPrLandingLivenessRead],
        publisher: ProtocolTickPublisher,
    ) -> None:
        self._handler_factory = handler_factory
        self._publisher = publisher

    def read(self, pr: str, head_sha: str) -> ModelTickLivenessRead:
        publisher = _BestEffortPublisher(self._publisher)
        try:
            result = self._handler_factory(publisher).handle(
                ModelLivenessReadRequest(
                    correlation_id=uuid4(), pr=pr, head_sha=head_sha
                )
            )
        except Exception as exc:
            return ModelTickLivenessRead(
                pr=pr,
                head_sha=head_sha,
                read=EnumLivenessRead.UNREADABLE,
                detail=f"the liveness read raised {type(exc).__name__}",
            )
        return ModelTickLivenessRead(
            pr=pr,
            head_sha=head_sha,
            read=result.liveness.read,
            undeclared_sources=result.liveness.undeclared_sources,
            detail=result.liveness.detail,
            topic=result.topic,
            publish_error=publisher.error,
        )


class UnavailableLivenessReader:
    """The reader of a pass whose liveness overlay could not be loaded: every read says so, none is made."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def read(self, pr: str, head_sha: str) -> ModelTickLivenessRead:
        return ModelTickLivenessRead(
            pr=pr,
            head_sha=head_sha,
            read=EnumLivenessRead.UNREADABLE,
            detail=self._detail,
        )


def build_liveness_reader(
    publisher: ProtocolTickPublisher, environ: Mapping[str, str] | None = None
) -> ProtocolTickLivenessReader:
    """The liveness read node's handler under its own overlay, publishing on the tick's publisher.

    An overlay that cannot be loaded does not stop the pass: each read then names why it was not made.
    """
    env = os.environ if environ is None else environ
    try:
        overlay = load_liveness_overlay(env)
    except Exception as exc:
        return UnavailableLivenessReader(
            f"the liveness read overlay did not load: {type(exc).__name__}: {str(exc)[:160]}"
        )
    reader = build_reader(overlay, env)

    def _handler(
        best_effort: _BestEffortPublisher,
    ) -> HandlerPrLandingLivenessRead:
        return HandlerPrLandingLivenessRead(overlay, reader, best_effort)

    return NodeLivenessTickReader(_handler, publisher)


__all__: list[str] = [
    "NodeLivenessTickReader",
    "UnavailableLivenessReader",
    "build_liveness_reader",
]
