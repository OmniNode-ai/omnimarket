"""Declarative compute node of a plugin script's node source (OMN-17427)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelScriptNodeSrcRequest, ModelScriptNodeSrcResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeScriptNodeSrcCompute:
    contract_path = CONTRACT

    def handle(self, request: ModelScriptNodeSrcRequest) -> ModelScriptNodeSrcResult:
        from .handler import HandlerScriptNodeSrc

        return HandlerScriptNodeSrc().handle(request)
