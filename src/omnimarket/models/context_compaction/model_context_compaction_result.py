# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one compaction invocation publishes (OMN-21113, TA.11).

A built package yields ``prepared``, ``activated`` and an ``install`` reply; any
failure before activation yields ``fallback`` and a ``native`` reply.
"""

from __future__ import annotations

from typing import Self

from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
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
        self,
    ) -> tuple[
        ModelContextCompactionPrepared
        | ModelContextCompactionActivated
        | ModelContextCompactionFallback
        | ModelContextCompactionPackageReply,
        ...,
    ]:
        """What to publish, in order: every receipt before the reply the adapter awaits."""
        receipts = (self.prepared, self.activated, self.fallback)
        return (*(r for r in receipts if r is not None), self.reply)


__all__ = ["ModelContextCompactionResult"]
