# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Response model for the read-only Contract Graph IR GET surface."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelContractGraphIrHashEntry", "ModelContractGraphIrResponse"]


class ModelContractGraphIrHashEntry(BaseModel):
    """Per-source hash manifest entry.

    Records the stable sha256 hashes for one imported source contract so diff
    evidence cannot drift between repeated requests. ``source_contract_sha256``
    covers the canonicalized source bytes; ``adapter_version_sha256`` covers
    the importing adapter's implementation — a behavioral change to an adapter
    changes this hash deterministically.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    source_path: str = Field(
        ...,
        description="Repo-relative path of the imported source contract",
        min_length=1,
    )
    dialect: str = Field(
        ...,
        description="Dialect adapter that imported this contract (e.g. 'node', 'ui_component')",
        min_length=1,
    )
    source_contract_sha256: str = Field(
        ...,
        description="sha256:<hex> over the canonicalized source contract bytes",
        min_length=8,
    )
    adapter_version_sha256: str = Field(
        ...,
        description="sha256:<hex> stable version hash of the importing adapter",
        min_length=8,
    )


class ModelContractGraphIrResponse(BaseModel):
    """Deterministic Contract Graph IR GET response.

    ``ir_json`` is the full ``ModelContractGraphIr`` serialized to JSON via
    ``model_dump_json()`` — byte-stable across repeated requests for the same
    inputs. ``hash_manifest`` is the ordered tuple of per-source / per-adapter
    sha256 hash entries extracted from ``ir.source_set.refs`` so callers can
    verify provenance without re-parsing the full IR. ``node_count`` and
    ``edge_count`` are convenience summary fields.

    This response is the authoritative GET surface for the Contract Graph IR;
    it carries no write path and performs no mutation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    ir_json: str = Field(
        ...,
        description="Full ModelContractGraphIr serialized as JSON (byte-stable across identical inputs)",
        min_length=1,
    )
    hash_manifest: tuple[ModelContractGraphIrHashEntry, ...] = Field(
        ...,
        description="Per-source / per-adapter sha256 hash manifest (deterministically ordered)",
    )
    node_count: int = Field(
        ...,
        description="Number of IR nodes imported",
        ge=0,
    )
    edge_count: int = Field(
        ...,
        description="Number of IR edges imported",
        ge=0,
    )
    discovery_roots: tuple[str, ...] = Field(
        ...,
        description="Repo-relative discovery roots that were scanned",
        min_length=1,
    )
