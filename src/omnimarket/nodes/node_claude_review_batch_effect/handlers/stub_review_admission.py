# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stand-in for the budget reducer's admission seam until it lands (OMN-20910).

The cost-shaped reviewer plan makes admission a durable reducer: it reserves a
call's maximum charge against a key's allowance and the aggregate monthly
budget, settles at measured usage and releases a call that was never
dispatched. That reducer is a separate lane's work. This stub keeps the same
three calls (admit, settle, release) over an in-process token ceiling, so the
batch effect is written against the seam and swapping the reducer in changes
one constructor argument. It holds no money, no key and no state outside the
process: it is not a budget, and nothing but a proof or a test may rely on it.
"""

from __future__ import annotations

from pydantic import BaseModel

from omnimarket.nodes.node_claude_review_batch_effect.models import (
    ModelBatchUsage,
    ModelReviewAdmissionDecision,
    ModelReviewAdmissionRequest,
)


class StubReviewAdmission:
    """Admits while the reserved tokens stay within ``ceiling_tokens``."""

    def __init__(self, ceiling_tokens: int) -> None:
        self._ceiling = ceiling_tokens
        self._reserved: dict[str, int] = {}

    def admit(
        self, request: ModelReviewAdmissionRequest
    ) -> ModelReviewAdmissionDecision:
        if request.item_id in self._reserved:
            return ModelReviewAdmissionDecision(
                item_id=request.item_id,
                admitted=True,
                reason="already reserved",
                reservation_id=f"stub-{request.item_id}",
            )
        worst_case = request.worst_case_input_tokens + request.max_tokens
        held = sum(self._reserved.values())
        if held + worst_case > self._ceiling:
            return ModelReviewAdmissionDecision(
                item_id=request.item_id,
                admitted=False,
                reason=f"stub ceiling: {held} reserved + {worst_case} > {self._ceiling}",
            )
        self._reserved[request.item_id] = worst_case
        return ModelReviewAdmissionDecision(
            item_id=request.item_id,
            admitted=True,
            reason="within the stub ceiling",
            reservation_id=f"stub-{request.item_id}",
        )

    def settle(self, item_id: str, usage: ModelBatchUsage) -> None:
        if item_id in self._reserved:
            self._reserved[item_id] = (
                usage.input_tokens
                + usage.cache_write_tokens
                + usage.cache_read_tokens
                + usage.output_tokens
            )

    def release(self, item_id: str) -> None:
        self._reserved.pop(item_id, None)


class RefusingReviewAdmission:
    """The default until a reducer is wired: refuses every item, so nothing is spent."""

    def admit(
        self, request: ModelReviewAdmissionRequest
    ) -> ModelReviewAdmissionDecision:
        return ModelReviewAdmissionDecision(
            item_id=request.item_id,
            admitted=False,
            reason="no admission is wired",
        )

    def settle(self, item_id: str, usage: ModelBatchUsage) -> None:
        return None

    def release(self, item_id: str) -> None:
        return None


class NullEventSink:
    """Drops per-item events; the terminal result still reaches the runtime's applier."""

    def publish(self, topic: str, event: BaseModel) -> None:
        return None


__all__ = ["NullEventSink", "RefusingReviewAdmission", "StubReviewAdmission"]
