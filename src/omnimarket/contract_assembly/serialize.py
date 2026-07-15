# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parent: serialize a node model into a full contract document.

Composes the four leaves -- render each selected subcontract (L1), resolve the
archetype advanced features (L2), assemble the document (L3), digest it (L4) --
and gates the result through the pure contract lint. The composition is itself a
pure, deterministic function; the parent node's handler is a thin wrapper over it.
"""

from __future__ import annotations

import re

from omnimarket.contract_assembly.advanced_features import resolve_advanced_features
from omnimarket.contract_assembly.assemble import assemble_contract
from omnimarket.contract_assembly.digest import digest_contract
from omnimarket.contract_assembly.lint import lint_contract
from omnimarket.contract_assembly.models import (
    ModelAdvancedFeaturesRequest,
    ModelContractAssembleRequest,
    ModelContractAssemblyRequest,
    ModelContractDigestRequest,
    ModelContractDocument,
    ModelContractMetadata,
    ModelSubcontractFragment,
    ModelSubcontractRenderRequest,
)
from omnimarket.contract_assembly.render import render_subcontract


def _service_name(node_name: str) -> str:
    """Derive a snake_case service name from a PascalCase node name."""

    name = node_name[4:] if node_name.startswith("Node") else node_name
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return name.lower()


def serialize_contract(
    request: ModelContractAssemblyRequest,
) -> ModelContractDocument:
    """Serialize a node model into a contract document, digest, and lint verdict."""

    fragments: tuple[ModelSubcontractFragment, ...] = tuple(
        render_subcontract(
            ModelSubcontractRenderRequest(
                type=selection.type,
                operations=selection.operations,
                extra_fields=selection.extra_fields,
            )
        )
        for selection in request.subcontract_selections
    )

    advanced_features = resolve_advanced_features(
        ModelAdvancedFeaturesRequest(
            archetype=request.archetype,
            overrides=request.overrides,
        )
    )

    service_name = _service_name(request.node_name)
    metadata = ModelContractMetadata(
        node_name=request.node_name,
        service_name=service_name,
        namespace=request.namespace,
        node_type=request.archetype.value.upper(),
        version=request.analysis.version,
        description=request.analysis.description or f"{service_name} node",
        tags=request.analysis.tags,
    )

    draft = assemble_contract(
        ModelContractAssembleRequest(
            metadata=metadata,
            fragments=fragments,
            advanced_features=advanced_features,
        )
    )
    digest = digest_contract(
        ModelContractDigestRequest(contract_yaml=draft.contract_yaml)
    )
    lint = lint_contract(draft.contract_yaml)

    return ModelContractDocument(
        contract_yaml=draft.contract_yaml,
        contract_sha256=digest.contract_sha256,
        subcontracts_rendered=fragments,
        lint_status=lint.status,
        lint_messages=lint.messages,
        correlation_id=request.correlation_id,  # OMN-14608: echo for reducer join
    )


__all__ = ["serialize_contract"]
