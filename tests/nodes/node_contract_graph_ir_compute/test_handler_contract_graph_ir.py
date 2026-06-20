# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the read-only Contract Graph IR GET surface (OMN-13385).

Covers (acceptance criteria from OMN-13385):
- GET returns a deterministic IR + hash manifest for >= 1 real backend node
  contract (effect) AND >= 1 backend node contract (compute).
- Hash manifest entries carry valid sha256:<hex> values.
- Repeated requests over the same inputs are byte-stable (determinism proof).
- Node/edge counts are consistent with manifest length.
- discovery_roots are echoed back in the response.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_contract_graph_ir_compute.handlers.handler_contract_graph_ir import (
    HandlerContractGraphIr,
)
from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_request import (
    ModelContractGraphIrRequest,
)
from omnimarket.nodes.node_contract_graph_ir_compute.models.model_contract_graph_ir_response import (
    ModelContractGraphIrResponse,
)

# Fixture source directory alongside this test file.
_FIXTURES_SRC = Path(__file__).resolve().parent / "fixtures"


def _make_ir_root(tmp_path: Path) -> Path:
    """Copy fixture contracts to a clean tmp_path subtree.

    ``discover_contract_paths`` excludes any path whose parts contain
    ``omni_worktrees``. Since this test file lives inside a worktree, the
    absolute path to the fixtures directory contains ``omni_worktrees`` and
    would be filtered. Copying to ``tmp_path`` (which pytest provides under
    ``/private/var/folders/…`` on macOS, never containing ``omni_worktrees``)
    ensures discovery operates cleanly on a real on-disk tree.
    """
    dest = tmp_path / "contracts"
    shutil.copytree(_FIXTURES_SRC, dest)
    return dest


def _make_request(tmp_path: Path) -> ModelContractGraphIrRequest:
    ir_root = _make_ir_root(tmp_path)
    return ModelContractGraphIrRequest(
        discovery_roots=(".",),
        repo_base_path=str(ir_root),
    )


@pytest.mark.unit
class TestHandlerContractGraphIrUnit:
    def test_returns_response_model(self, tmp_path: Path) -> None:
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert isinstance(response, ModelContractGraphIrResponse)

    def test_ir_json_is_non_empty(self, tmp_path: Path) -> None:
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert len(response.ir_json) > 0

    def test_at_least_one_backend_node_contract_imported(self, tmp_path: Path) -> None:
        """IR contains >= 1 real backend node contract (effect + compute).

        The fixtures directory ships two real contract.yaml files:
          - node_effect_a/contract.yaml (node_type: effect)
          - node_compute_b/contract.yaml (node_type: compute)

        Both must be imported into the IR.
        """
        import json

        from omnibase_core.enums.enum_contract_graph_node_role import (
            EnumContractGraphNodeRole,
        )

        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert response.node_count >= 2, (
            f"Expected >= 2 nodes (one effect + one compute), got {response.node_count}"
        )

        ir_data = json.loads(response.ir_json)
        roles = {n["role"] for n in ir_data["nodes"]}
        assert EnumContractGraphNodeRole.EFFECT.value in roles, (
            f"Expected EFFECT role in IR nodes. Found roles: {roles}"
        )
        assert EnumContractGraphNodeRole.COMPUTE.value in roles, (
            f"Expected COMPUTE role in IR nodes. Found roles: {roles}"
        )

    def test_hash_manifest_entries_have_valid_sha256(self, tmp_path: Path) -> None:
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert len(response.hash_manifest) >= 1
        for entry in response.hash_manifest:
            assert entry.source_contract_sha256.startswith("sha256:"), (
                f"source_contract_sha256 must start with 'sha256:': "
                f"{entry.source_contract_sha256}"
            )
            assert entry.adapter_version_sha256.startswith("sha256:"), (
                f"adapter_version_sha256 must start with 'sha256:': "
                f"{entry.adapter_version_sha256}"
            )

    def test_hash_manifest_length_equals_node_count(self, tmp_path: Path) -> None:
        """One hash manifest entry per imported node."""
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert len(response.hash_manifest) == response.node_count

    def test_discovery_roots_echoed_in_response(self, tmp_path: Path) -> None:
        request = _make_request(tmp_path)
        response = HandlerContractGraphIr().handle(request)
        assert response.discovery_roots == request.discovery_roots

    def test_node_and_edge_counts_consistent_with_ir_json(self, tmp_path: Path) -> None:
        import json

        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        ir_data = json.loads(response.ir_json)
        assert len(ir_data["nodes"]) == response.node_count
        assert len(ir_data["edges"]) == response.edge_count

    def test_determinism_repeated_request_is_byte_stable(self, tmp_path: Path) -> None:
        """Two identical requests must produce byte-for-byte identical ir_json.

        This is the core acceptance criterion from OMN-13385: a repeated GET
        is byte-stable, proving the IR is purely derived from source contract
        bytes with no clock/env contamination.
        """
        handler = HandlerContractGraphIr()
        ir_root = _make_ir_root(tmp_path)
        request = ModelContractGraphIrRequest(
            discovery_roots=(".",),
            repo_base_path=str(ir_root),
        )
        response_1 = handler.handle(request)
        response_2 = handler.handle(request)

        assert response_1.ir_json == response_2.ir_json, (
            "IR JSON is not byte-stable across identical requests"
        )

    def test_determinism_hash_manifest_is_stable(self, tmp_path: Path) -> None:
        """Hash manifest entries are identical across repeated requests."""
        handler = HandlerContractGraphIr()
        ir_root = _make_ir_root(tmp_path)
        request = ModelContractGraphIrRequest(
            discovery_roots=(".",),
            repo_base_path=str(ir_root),
        )
        r1 = handler.handle(request)
        r2 = handler.handle(request)

        assert r1.hash_manifest == r2.hash_manifest, (
            "Hash manifest is not stable across identical requests"
        )

    def test_determinism_node_edge_counts_stable(self, tmp_path: Path) -> None:
        handler = HandlerContractGraphIr()
        ir_root = _make_ir_root(tmp_path)
        request = ModelContractGraphIrRequest(
            discovery_roots=(".",),
            repo_base_path=str(ir_root),
        )
        r1 = handler.handle(request)
        r2 = handler.handle(request)
        assert r1.node_count == r2.node_count
        assert r1.edge_count == r2.edge_count

    def test_empty_discovery_root_returns_empty_ir(self, tmp_path: Path) -> None:
        """An empty directory produces an empty IR with zero nodes."""
        request = ModelContractGraphIrRequest(
            discovery_roots=(".",),
            repo_base_path=str(tmp_path),
        )
        response = HandlerContractGraphIr().handle(request)
        assert response.node_count == 0
        assert response.edge_count == 0
        assert response.hash_manifest == ()

    def test_ir_json_is_valid_json(self, tmp_path: Path) -> None:
        import json

        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        parsed = json.loads(response.ir_json)
        assert "nodes" in parsed
        assert "edges" in parsed
        assert "source_set" in parsed
        assert "ir_version" in parsed

    def test_dialect_field_in_hash_manifest(self, tmp_path: Path) -> None:
        """All hash manifest entries for node contracts carry dialect='node'."""
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        for entry in response.hash_manifest:
            assert entry.dialect == "node", (
                f"Expected dialect='node' for backend contract at {entry.source_path}, "
                f"got {entry.dialect!r}"
            )

    def test_fixture_contracts_appear_in_ir_by_handler_id(self, tmp_path: Path) -> None:
        """Both fixture contracts appear in the IR by their declared handler_id."""
        import json

        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        ir_data = json.loads(response.ir_json)
        node_ids = {n["node_id"] for n in ir_data["nodes"]}

        assert "node.test.effect.a" in node_ids, (
            f"effect fixture node.test.effect.a not found in IR. Found: {sorted(node_ids)}"
        )
        assert "node.test.compute.b" in node_ids, (
            f"compute fixture node.test.compute.b not found in IR. Found: {sorted(node_ids)}"
        )

    def test_hash_manifest_source_paths_are_repo_relative(self, tmp_path: Path) -> None:
        """Hash manifest source_path values are repo-relative (not absolute)."""
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        for entry in response.hash_manifest:
            assert not entry.source_path.startswith("/"), (
                f"source_path should be repo-relative, got: {entry.source_path!r}"
            )

    def test_per_source_sha256_differs_between_contracts(self, tmp_path: Path) -> None:
        """Different contracts produce different source_contract_sha256 values."""
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        assert response.node_count >= 2
        sha256s = [e.source_contract_sha256 for e in response.hash_manifest]
        assert len(sha256s) == len(set(sha256s)), (
            "source_contract_sha256 values should be unique per contract"
        )

    def test_edges_present_for_contracts_with_topics(self, tmp_path: Path) -> None:
        """Contracts with publish/subscribe topics produce edges in the IR."""
        response = HandlerContractGraphIr().handle(_make_request(tmp_path))
        # Both fixture contracts declare publish_topics and subscribe_topics
        # so edge_count must be > 0
        assert response.edge_count > 0, (
            "Expected edges from fixture contracts with topic declarations"
        )
