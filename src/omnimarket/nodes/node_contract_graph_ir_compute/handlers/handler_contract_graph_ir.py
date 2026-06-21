# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for node_contract_graph_ir_compute (OMN-13385).

COMPUTE node: read-only GET surface for the deterministic Contract Graph IR.

Manifest-driven discovery finds all ``contract.yaml`` files under the given
discovery roots (excluding .venv, omni_worktrees, generated surfaces), imports
them into the canonical ``ModelContractGraphIr`` via the Phase-2 IR adapters
(``omnibase_core.contract_graph``), and returns the IR JSON + stable per-source
/ per-adapter sha256 hash manifest.

STRICTLY READ-ONLY: no mutation, no authoring logic, no write path.

Per ONEX rules: COMPUTE returns ``result`` (required). Forbidden: events,
intents, projections.
"""

from __future__ import annotations

from pathlib import Path

from omnibase_core.contract_graph import (
    discover_contract_paths,
    import_paths,
    supports_node_contract,
)
from omnibase_core.contract_graph.importer import _load_yaml

from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_request import (
    ModelContractGraphIrRequest,
)
from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_response import (
    ModelContractGraphIrHashEntry,
    ModelContractGraphIrResponse,
)

__all__ = ["HandlerContractGraphIr"]


def _build_response(
    request: ModelContractGraphIrRequest,
) -> ModelContractGraphIrResponse:
    """Import contracts under discovery_roots and return the deterministic IR response.

    Discovery roots are resolved against ``repo_base_path`` to real filesystem
    paths. Only ``contract.yaml`` files recognized by the node-contract dialect
    adapter are imported (UI component contracts are in-memory objects; they are
    not emitted by discovery of on-disk files in this surface).
    """
    repo_base = Path(request.repo_base_path)

    fs_roots = tuple(repo_base / root for root in request.discovery_roots)
    discovered = discover_contract_paths(fs_roots)

    node_paths: list[tuple[Path, str]] = []
    for fs_path in discovered:
        try:
            data = _load_yaml(fs_path)
        except (ValueError, OSError):
            continue
        if not supports_node_contract(data):
            continue
        # repo-relative path: strip the repo_base prefix
        try:
            repo_relative = str(fs_path.relative_to(repo_base))
        except ValueError:
            repo_relative = str(fs_path)
        node_paths.append((fs_path, repo_relative))

    # UI component contracts are in-memory primitives, not on-disk contract.yaml
    # files, so they carry a stable in-memory source_path derived from their id.
    ui_components = tuple(
        (component, f"in-memory://ui_component/{component.component_id}")
        for component in request.ui_components
    )

    ir = import_paths(
        discovery_roots=request.discovery_roots,
        node_paths=tuple(node_paths),
        ui_components=ui_components,
    )

    hash_manifest = tuple(
        ModelContractGraphIrHashEntry(
            source_path=ref.source_path,
            dialect=ref.dialect,
            source_contract_sha256=ref.source_contract_sha256,
            adapter_version_sha256=ref.adapter_version_sha256,
        )
        for ref in ir.source_set.refs
    )

    return ModelContractGraphIrResponse(
        ir_json=ir.model_dump_json(),
        hash_manifest=hash_manifest,
        node_count=len(ir.nodes),
        edge_count=len(ir.edges),
        discovery_roots=request.discovery_roots,
    )


class HandlerContractGraphIr:
    """ONEX COMPUTE handler: read-only Contract Graph IR GET surface."""

    def handle(
        self, request: ModelContractGraphIrRequest
    ) -> ModelContractGraphIrResponse:
        """Return the deterministic Contract Graph IR + hash manifest."""
        return _build_response(request)


__all__ = ["HandlerContractGraphIr", "_build_response"]
