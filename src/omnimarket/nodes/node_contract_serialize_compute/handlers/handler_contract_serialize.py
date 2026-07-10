"""Thin COMPUTE handler over the shared model-to-contract serializer.

The parent composes the four leaves (render, resolve, assemble, digest) and the
contract-lint gate; the composition is a pure function in the shared library, so
this handler stays thin and imports no sibling node.
"""

from __future__ import annotations

from omnimarket.contract_assembly.models import (
    ModelContractAssemblyRequest,
    ModelContractDocument,
)
from omnimarket.contract_assembly.serialize import serialize_contract


class HandlerContractSerialize:
    """Serialize a node model into a contract document, digest, and lint verdict."""

    def handle(self, payload: ModelContractAssemblyRequest) -> ModelContractDocument:
        return serialize_contract(payload)


__all__ = ["HandlerContractSerialize"]
