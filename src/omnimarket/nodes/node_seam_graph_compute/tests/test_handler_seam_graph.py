# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerSeamGraph (node_seam_graph_compute, OMN-15763).

Covers: contract-declared ``seams:`` block extraction, code-level extraction
(Kafka producer/consumer topic args, ``os.environ`` reads, ``@ref`` pins),
and AC7 determinism — two runs over the same pinned fixture tree emit a
byte-identical ``seam-graph/v2`` graph and an identical per-source sha256
manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_seam_graph_compute.handlers.handler_seam_graph import (
    HandlerSeamGraph,
)
from omnimarket.nodes.node_seam_graph_compute.models.model_seam_graph_extraction_request import (
    ModelSeamGraphExtractionRequest,
)
from omnimarket.seams.models.model_seam_graph import EnumSeamGraphObservationKind


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` to the nearest ancestor carrying pyproject.toml.

    The fixture tree lives under top-level ``tests/nodes/`` (not this node's
    own ``src/omnimarket/nodes/.../tests/`` — canonical_handler_shape.py's
    src-scoped node-discovery scan would otherwise treat the fixture's bare
    ``contracts/contract.yaml`` as a real, non-canonical node), so this test
    file needs a robust cross-directory reference rather than a fragile
    hardcoded ``parents[N]`` count.
    """
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(f"could not locate repo root (pyproject.toml) above {start}")


_FIXTURE_REPO_BASE = (
    _find_repo_root(Path(__file__).resolve())
    / "tests"
    / "nodes"
    / "node_seam_graph_compute"
    / "fixtures"
    / "repo_a"
)


def _request(
    discovery_roots: tuple[str, ...] = ("svc_producer",),
) -> ModelSeamGraphExtractionRequest:
    return ModelSeamGraphExtractionRequest(
        repo_base_path=str(_FIXTURE_REPO_BASE),
        discovery_roots=discovery_roots,
    )


@pytest.mark.unit
class TestContractDeclaredExtraction:
    def test_extracts_declared_seam_edge_from_contract_yaml(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        assert graph.schema_version == "seam-graph/v2"
        edge_ids = {edge.edge_id for edge in graph.edges}
        assert "S1" in edge_ids
        edge = next(e for e in graph.edges if e.edge_id == "S1")
        assert edge.topic == (
            "tenant-{slug}.onex.cmd.omnibase-infra.delegation-request.v1"
        )
        assert edge.envelope_version == "1.0.0"


@pytest.mark.unit
class TestCodeLevelExtraction:
    def test_extracts_producer_send_topic_literal(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        values = {
            obs.value
            for obs in graph.code_observations
            if obs.kind == EnumSeamGraphObservationKind.PRODUCER_SEND
        }
        assert "tenant-x.onex.evt.example-produced.v1" in values

    def test_extracts_consumer_subscribe_topic_literal(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        values = {
            obs.value
            for obs in graph.code_observations
            if obs.kind == EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE
        }
        assert "tenant-x.onex.cmd.example-consumed.v1" in values

    def test_extracts_env_var_read(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        values = {
            obs.value
            for obs in graph.code_observations
            if obs.kind == EnumSeamGraphObservationKind.ENV_READ
        }
        assert "FIXTURE_ENDPOINT_URL" in values

    def test_extracts_ref_pin(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        values = {
            obs.value
            for obs in graph.code_observations
            if obs.kind == EnumSeamGraphObservationKind.REF_PIN
        }
        assert "configs/service_endpoints.yaml#backends.cloud-gemini-pro" in values


@pytest.mark.unit
class TestDeterminism:
    def test_two_runs_are_byte_identical_graph(self) -> None:
        first = HandlerSeamGraph().handle(_request())
        second = HandlerSeamGraph().handle(_request())
        assert first.model_dump_json() == second.model_dump_json()

    def test_two_runs_have_identical_source_manifest(self) -> None:
        first = HandlerSeamGraph().handle(_request())
        second = HandlerSeamGraph().handle(_request())
        assert first.source_manifest == second.source_manifest
        assert len(first.source_manifest) > 0

    def test_source_manifest_is_sorted_by_source_path(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        paths = [entry.source_path for entry in graph.source_manifest]
        assert paths == sorted(paths)

    def test_canonical_output_round_trips_through_json(self) -> None:
        graph = HandlerSeamGraph().handle(_request())
        assert json.loads(graph.model_dump_json()) == graph.model_dump(mode="json")
