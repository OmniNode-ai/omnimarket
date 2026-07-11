# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L4: compute the stable content digest of a serialized contract document.

The digest is the sha256 of the exact contract-YAML bytes it is given -- the
canonical form (header, key order, formatting) is the assembler's responsibility,
so the digest is a pure, deterministic function of its input. This mirrors the
``contract_sha256`` convention used by the change-control receipt flow.
"""

from __future__ import annotations

import hashlib

from omnimarket.contract_assembly.models import (
    ModelContractDigest,
    ModelContractDigestRequest,
)


def digest_contract(request: ModelContractDigestRequest) -> ModelContractDigest:
    """Return the sha256 digest of the serialized contract YAML."""

    sha256 = hashlib.sha256(request.contract_yaml.encode("utf-8")).hexdigest()
    return ModelContractDigest(contract_sha256=sha256)


__all__ = ["digest_contract"]
