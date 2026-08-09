# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Top-level contract-state-coverage smoke test for node_seam_graph_compute
and node_seam_match_compute (OMN-15763).

``scripts/validate_state_coverage.py``'s test corpus is the repo-root
``tests/`` tree only — it does not discover node-local
``src/omnimarket/nodes/node_*/tests/`` directories (confirmed live: pre-existing
nodes such as ``node_chain_diff_compute`` pass that gate today only because
some unrelated top-level test happens to reference the same common field
name, e.g. ``.matches``, by coincidence — not because its own node-local
suite is scanned). Both nodes' node-local suites
(``src/omnimarket/nodes/node_seam_*_compute/tests/``) are the real,
thorough behavioral coverage; this file is the top-level companion that
makes every declared ``contract.yaml`` output name a genuine, non-vacuous
attribute reference the gate can see, end to end through the real handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_graph_compute.handlers.handler_seam_graph import (
    HandlerSeamGraph,
)
from omnimarket.nodes.node_seam_graph_compute.models.model_seam_graph_extraction_request import (
    ModelSeamGraphExtractionRequest,
)
from omnimarket.nodes.node_seam_match_compute.handlers.handler_seam_match import (
    HandlerSeamMatch,
)
from omnimarket.nodes.node_seam_match_compute.models.model_seam_match_request import (
    ModelSeamMatchRequest,
)
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamProjectionRole,
    ModelSeamProjection,
)


def _load_contract(node_name: str) -> dict[str, object]:
    path = Path("src/omnimarket/nodes") / node_name / "contract.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.unit
def test_seam_match_verdict_covers_every_declared_output_field() -> None:
    producer = ModelSeamProjection(
        edge_id="S1",
        role=EnumSeamProjectionRole.PRODUCER,
        topic="tenant-x.onex.cmd.omnibase-infra.delegation-request.v1",
        envelope_model="omnibase_core.models.wire.model_delegation_routing_input.ModelDelegationRoutingInput",
        envelope_version="1.0.0",
    )
    consumer = ModelSeamProjection(
        edge_id="S1",
        role=EnumSeamProjectionRole.CONSUMER,
        topic="tenant-x.onex.cmd.omnibase-infra.delegation-request.v1",
        envelope_model="omnibase_core.models.wire.model_delegation_routing_input.ModelDelegationRoutingInput",
        envelope_version="1.0.0",
    )
    request = ModelSeamMatchRequest(
        edge_id="S1",
        declared_producer=producer,
        declared_consumer=consumer,
        observed_producer=producer,
        observed_consumer=consumer,
    )
    verdict = HandlerSeamMatch().handle(request)

    # Every field named in node_seam_match_compute/contract.yaml `outputs:`,
    # exercised as a real attribute access against a real handler result.
    assert verdict.edge_id == "S1"
    assert verdict.verdict is not None
    assert verdict.regenerability is not None
    assert verdict.leg1_declared_vs_declared.passed is True
    assert verdict.leg2_observed_producer_vs_declared.passed is True
    assert verdict.leg3_observed_consumer_vs_declared.passed is True
    assert verdict.declared_producer_hash is not None
    assert verdict.declared_consumer_hash is not None

    contract = _load_contract("node_seam_match_compute")
    terminal_event = contract["terminal_event"]
    assert isinstance(terminal_event, str)
    assert terminal_event == "onex.evt.omnimarket.seam-match-completed.v1"


@pytest.mark.unit
def test_seam_graph_output_covers_every_declared_output_field() -> None:
    fixture_repo_base = Path(
        "src/omnimarket/nodes/node_seam_graph_compute/tests/fixtures/repo_a"
    )
    request = ModelSeamGraphExtractionRequest(
        repo_base_path=str(fixture_repo_base),
        discovery_roots=("svc_producer",),
    )
    graph = HandlerSeamGraph().handle(request)

    # Every field named in node_seam_graph_compute/contract.yaml `outputs:`,
    # exercised as a real attribute access against a real handler result.
    assert graph.schema_version == "seam-graph/v1"
    assert len(graph.edges) > 0
    assert len(graph.code_observations) > 0
    assert len(graph.source_manifest) > 0

    contract = _load_contract("node_seam_graph_compute")
    terminal_event = contract["terminal_event"]
    assert isinstance(terminal_event, str)
    assert terminal_event == "onex.evt.omnimarket.seam-graph-computed.v1"
