# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one compaction invocation publishes (OMN-21113, TA.11).

A built package yields ``prepared``, ``activated`` and an ``install`` reply; any
failure before activation yields ``fallback`` and a ``native`` reply.

``prepared``, ``fallback`` and the reply are published to the per-principal topics
the command names (OMN-22103), so :meth:`emissions` wraps each in an envelope whose
``event_type`` is that full topic; ``activated`` stays on its static contract topic.
"""

from __future__ import annotations

from typing import Any, Self

from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_compaction.model_context_compaction_activated import (
    ModelContextCompactionActivated,
)
from omnimarket.models.context_compaction.model_context_compaction_fallback import (
    ModelContextCompactionFallback,
)
from omnimarket.models.context_compaction.model_context_compaction_package_reply import (
    ModelContextCompactionPackageReply,
)
from omnimarket.models.context_compaction.model_context_compaction_prepared import (
    ModelContextCompactionPrepared,
)
from omnimarket.models.context_compaction.model_context_compaction_requested import (
    ModelContextCompactionRequested,
)


def _to_topic[T: BaseModel](
    topic: str, payload: T, request: ModelContextCompactionRequested
) -> ModelEventEnvelope[T]:
    return ModelEventEnvelope(
        payload=payload, event_type=topic, correlation_id=request.invocation_id
    )


class ModelContextCompactionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reply: ModelContextCompactionPackageReply
    prepared: ModelContextCompactionPrepared | None = None
    activated: ModelContextCompactionActivated | None = None
    fallback: ModelContextCompactionFallback | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if self.reply.action is EnumClientVerdictAction.INSTALL:
            ok = (
                self.prepared is not None
                and self.activated is not None
                and self.fallback is None
            )
        else:
            ok = self.activated is None and self.fallback is not None
        if not ok:
            raise ValueError(
                "install publishes prepared and activated; native publishes fallback"
            )
        return self

    def emissions(
        self, request: ModelContextCompactionRequested
    ) -> tuple[ModelEventEnvelope[Any] | ModelContextCompactionActivated, ...]:
        """What to publish, in order: every receipt before the reply the adapter awaits.

        Prepared, fallback and the reply travel in an envelope naming the topic the
        ``request`` asked for; activated is returned bare and routed by class.
        """
        out: list[ModelEventEnvelope[Any] | ModelContextCompactionActivated] = []
        if self.prepared is not None:
            out.append(_to_topic(request.reply_topic_prepared, self.prepared, request))
        if self.activated is not None:
            out.append(self.activated)
        if self.fallback is not None:
            out.append(_to_topic(request.reply_topic_fallback, self.fallback, request))
        out.append(_to_topic(request.reply_topic_package, self.reply, request))
        return tuple(out)


__all__ = ["ModelContextCompactionResult"]
