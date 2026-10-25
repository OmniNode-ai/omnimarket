"""Declarative compute node of the green PR draft clock (OMN-19998)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .handler import green_since as green_since

if TYPE_CHECKING:
    from .models import ModelGreenDraftClockRequest, ModelGreenDraftClockResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeGreenDraftClockCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelGreenDraftClockRequest
    ) -> ModelGreenDraftClockResult:
        from .handler import HandlerGreenDraftClock

        return HandlerGreenDraftClock().handle(request)


def draft_clock(
    previous: Mapping[str, Any] | None,
    drafts_now: Mapping[str, bool],
    now: datetime,
) -> tuple[dict[str, str], dict[str, datetime]]:
    from .models import ModelGreenDraftClockRequest

    result = NodeGreenDraftClockCompute().handle(
        ModelGreenDraftClockRequest(previous, drafts_now, now)
    )
    return result.drafts, result.ready_since
