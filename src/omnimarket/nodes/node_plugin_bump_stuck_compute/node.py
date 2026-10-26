"""Declarative compute node of the stuck plugin version bump judgement (OMN-20497)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelPluginBumpStuckRequest, ModelPluginBumpStuckResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodePluginBumpStuckCompute:
    contract_path = CONTRACT

    def handle(
        self, request: ModelPluginBumpStuckRequest
    ) -> ModelPluginBumpStuckResult:
        from .handler import HandlerPluginBumpStuck

        return HandlerPluginBumpStuck().handle(request)
