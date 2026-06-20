# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical input/output contract signature for tool-reuse matching (OMN-13356).

The signature is the deterministic key used for exact reuse matching. Two tools
with the same ``input_fields_hash`` and ``output_fields_hash`` accept and emit
structurally identical payloads and are reuse-compatible by construction.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field


def compute_fields_hash(fields: dict[str, str]) -> str:
    """Order-independent SHA-256 over ``{field_name: type_name}`` pairs.

    Deterministic and reproducible across runs: the input mapping is sorted by
    field name before hashing so field-declaration order does not affect the
    digest. Returned as ``sha256:<hex>`` to make the algorithm explicit at the
    call site.
    """

    canonical = "\n".join(
        f"{name}:{type_name}" for name, type_name in sorted(fields.items())
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class ModelInputOutputSignature(BaseModel):
    """Contract signature of a tool's input and output models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_model_name: str = Field(min_length=1, description="Input model class name")
    input_model_module: str = Field(min_length=1, description="Input model module path")
    output_model_name: str = Field(min_length=1, description="Output model class name")
    output_model_module: str = Field(
        min_length=1, description="Output model module path"
    )
    input_fields_hash: str = Field(
        min_length=1,
        description="sha256:<hex> of input model field {name: type} pairs (order-independent)",
    )
    output_fields_hash: str = Field(
        min_length=1,
        description="sha256:<hex> of output model field {name: type} pairs (order-independent)",
    )


__all__ = ["ModelInputOutputSignature", "compute_fields_hash"]
