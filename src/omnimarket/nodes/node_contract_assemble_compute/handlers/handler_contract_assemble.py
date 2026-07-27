"""Thin COMPUTE handler over the shared contract assembler."""

from __future__ import annotations

from omnimarket.contract_assembly.assemble import assemble_contract
from omnimarket.contract_assembly.models import (
    ModelContractAssembleRequest,
    ModelContractDraft,
)


class HandlerContractAssemble:
    """Assemble metadata + fragments + advanced features into contract YAML."""

    def handle(self, payload: ModelContractAssembleRequest) -> ModelContractDraft:
        return assemble_contract(payload)


__all__ = ["HandlerContractAssemble"]
