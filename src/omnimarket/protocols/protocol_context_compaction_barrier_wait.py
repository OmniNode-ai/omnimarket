# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session-barrier wait the compaction orchestrator runs first (OMN-21113, TA.11).

Returns the barrier verdict at the moment the wait ended: ``met`` or
``disabled`` as soon as either holds, otherwise the last unmet verdict at the
deadline. An exception from it is classed ``build_error`` by the caller.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.context_compaction.model_context_compaction_requested import (
    ModelContextCompactionRequested,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierVerdict,
)


class ProtocolContextCompactionBarrierWait(Protocol):
    def wait(
        self, request: ModelContextCompactionRequested
    ) -> ModelSessionStreamBarrierVerdict: ...


__all__: list[str] = ["ProtocolContextCompactionBarrierWait"]
