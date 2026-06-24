# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for node_data_flow_sweep dispatch-path resolution (OMN-13534).

Guards the trust-defeating dispatch defect in the same class as OMN-13514
(compliance_sweep false-clean): invoked the operator-canonical no-arg way
(``onex skill data_flow_sweep`` -> RuntimeLocal dispatch), the node crashed.

Root cause: the ``onex skill data_flow_sweep`` mapping (and the node
``contract.yaml``) supply a ``collect`` field, but ``DataFlowSweepRequest`` only
declared ``flows`` + ``dry_run`` with ``extra="forbid"`` — so the default
dispatch payload (``{"flows": [], "collect": False, "dry_run": False}``) was
rejected with ``extra_forbidden`` and the prescribed skill invocation could not
run at all. ``collect`` lived only as a ``__main__`` argparse flag, so the live
collection never happened on the dispatch path either.

Fix: ``collect`` is now a real ``DataFlowSweepRequest`` field, and the handler's
``resolve_flows`` resolves the built-in critical-chain descriptors
(live-collected when ``collect`` is set) on BOTH the CLI and the dispatch path,
identically.

These tests assert:

1. The exact default skill-mapping payload no longer crashes.
2. A no-arg dispatch resolves a non-empty flow set (no false-clean over zero
   flows).
3. ``collect=True`` exercises the live collector on the dispatch path (not only
   ``__main__``).
4. The contract / skill-mapping / request-model ``collect`` field stays
   reconciled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    DataFlowSweepRequest,
    EnumProducerStatus,
    ModelFlowInput,
    NodeDataFlowSweep,
    resolve_flows,
)

# Exact payload the omnibase_infra skill_mapping builds for a no-arg
# ``onex skill data_flow_sweep`` (each arg has a default, so all three keys are
# always populated). Kept verbatim as the regression fixture (OMN-13534).
_SKILL_MAPPING_DEFAULT_PAYLOAD: dict[str, object] = {
    "flows": [],
    "collect": False,
    "dry_run": False,
}


@pytest.mark.unit
class TestDispatchDoesNotCrash:
    def test_default_skill_payload_constructs(self) -> None:
        """The default skill-mapping payload no longer raises extra_forbidden."""
        request = DataFlowSweepRequest(**_SKILL_MAPPING_DEFAULT_PAYLOAD)
        assert request.collect is False
        assert request.flows == []

    def test_no_arg_dispatch_returns_real_result(self) -> None:
        """No-arg dispatch resolves the built-in descriptors, not a false-clean zero set."""
        request = DataFlowSweepRequest(**_SKILL_MAPPING_DEFAULT_PAYLOAD)
        result = NodeDataFlowSweep().handle(request)
        assert result.flows_checked == 3
        # Zero-value descriptor defaults classify as broken, so a no-arg dispatch
        # must NOT report healthy/clean over nothing — it reflects real topology.
        assert result.status == "issues_found"


@pytest.mark.unit
class TestResolveFlowsPrecedence:
    def test_explicit_flows_take_precedence(self) -> None:
        explicit = ModelFlowInput(
            topic="onex.evt.test.explicit.v1",
            handler_name="projectExplicit",
            table_name="explicit_table",
        )
        request = DataFlowSweepRequest(flows=[explicit], collect=True)
        # Even with collect=True, an explicit flows list wins and skips
        # collection.
        resolved = resolve_flows(request)
        assert resolved == [explicit]

    def test_empty_no_collect_resolves_default_stubs(self) -> None:
        resolved = resolve_flows(DataFlowSweepRequest(flows=[], collect=False))
        assert len(resolved) == 3

    def test_collect_runs_live_collector_on_dispatch_path(self) -> None:
        """collect=True invokes the live collector on the dispatch path (OMN-13534)."""
        populated = ModelFlowInput(
            topic="onex.evt.platform.node-introspection.v1",
            handler_name="projectNodeIntrospection",
            table_name="node_service_registry",
            producer_status=EnumProducerStatus.ACTIVE,
            consumer_lag=0,
            table_row_count=7,
            table_has_recent_data=True,
        )
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.assert_lane_reachable",
                return_value=None,
            ),
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
                return_value=populated,
            ) as mock_collect,
        ):
            resolved = resolve_flows(DataFlowSweepRequest(flows=[], collect=True))
        assert mock_collect.called
        assert all(f.producer_status == EnumProducerStatus.ACTIVE for f in resolved)

    def test_collect_per_flow_failure_falls_back_to_descriptor(self) -> None:
        """A single failing probe falls back to the descriptor, not an abort."""
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.assert_lane_reachable",
                return_value=None,
            ),
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
                side_effect=RuntimeError("rpk unavailable"),
            ),
        ):
            resolved = resolve_flows(DataFlowSweepRequest(flows=[], collect=True))
        # All three descriptors returned (unpopulated) — sweep did not abort.
        assert len(resolved) == 3


@pytest.mark.unit
class TestContractMappingReconciliation:
    def test_contract_declares_collect_input(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_data_flow_sweep"
            / "contract.yaml"
        )
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        assert "collect" in contract["inputs"]

    def test_request_model_accepts_collect_and_flows(self) -> None:
        fields = set(DataFlowSweepRequest.model_fields.keys())
        # The skill_mapping surfaces exactly these payload fields.
        assert {"flows", "collect", "dry_run"} <= fields
