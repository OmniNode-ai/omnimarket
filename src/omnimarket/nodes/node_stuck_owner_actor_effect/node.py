"""Declarative effect node of the stuck-owner actor (OMN-17427)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelStuckOwnerRequest, ModelStuckOwnerResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeStuckOwnerActorEffect:
    contract_path = CONTRACT

    def handle(self, request: ModelStuckOwnerRequest) -> ModelStuckOwnerResult:
        from .handler import HandlerStuckOwnerActor

        return HandlerStuckOwnerActor().handle(request)
