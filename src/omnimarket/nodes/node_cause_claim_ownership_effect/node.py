"""Declarative effect node of the shared-red cause-claim ownership rule (OMN-19998)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelCauseClaimOwnershipRequest, ModelCauseClaimOwnershipResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeCauseClaimOwnershipEffect:
    contract_path = CONTRACT

    def handle(
        self, request: ModelCauseClaimOwnershipRequest
    ) -> ModelCauseClaimOwnershipResult:
        from .handler import HandlerCauseClaimOwnership

        return HandlerCauseClaimOwnership().handle(request)
