"""Thin COMPUTE handler over the shared contract digester."""

from __future__ import annotations

from omnimarket.contract_assembly.digest import digest_contract
from omnimarket.contract_assembly.models import (
    ModelContractDigest,
    ModelContractDigestRequest,
)


class HandlerContractDigest:
    """Compute the stable sha256 digest of a serialized contract document."""

    def handle(self, payload: ModelContractDigestRequest) -> ModelContractDigest:
        return digest_contract(payload)


__all__ = ["HandlerContractDigest"]
