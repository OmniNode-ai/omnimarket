# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for live-path nodes touched by the OMN-13530 operation-field sweep.

OMN-13530 added the required ``operation`` field to every ``operation_match`` (and
absent-strategy) handler_routing entry so contracts stop failing closed at RuntimeLocal
startup with ``handlers[i].operation is missing``. The change is mechanical (a routing
field, no handler behavior change), but it touches these live-path node contracts, so the
golden-chain-coverage-gate requires coverage. This parametrized test asserts each touched
contract still parses, declares routing, and — for operation_match contracts — that every
handler entry now carries a non-empty ``operation`` (the exact regression this ticket
fixes). Mirrors tests/test_golden_chain_release_node_contract_coverage.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

# Live-path nodes whose contracts gained an `operation` field in OMN-13530 and that
# lacked a bespoke golden-chain test. Nodes with their own behavioral golden-chain test
# (e.g. node_auto_merge_effect -> test_golden_chain_auto_merge_effect.py) are NOT listed
# here — they are covered by their dedicated test.
OMN_13530_TOUCHED_NODES = (
    "node_ab_compare_orchestrator",
    "node_ab_compare_reducer",
    "node_ab_inference_effect",
    "node_authorize",
    "node_content_ingestion_effect",
    "node_full_triage_orchestrator",
    "node_kafka_topic_emit_probe",
    "node_omnigate_receipt_generator",
    "node_session_phase_dispatcher",
    "node_swarm_endpoint_health_effect",
    "node_worker_stall_recovery",
)


@pytest.mark.unit
@pytest.mark.parametrize("node_name", OMN_13530_TOUCHED_NODES)
def test_operation_field_touched_node_contract_is_well_formed(node_name: str) -> None:
    node_dir = NODES_ROOT / node_name
    contract_path = node_dir / "contract.yaml"
    metadata_path = node_dir / "metadata.yaml"

    assert node_dir.is_dir()
    assert contract_path.is_file()
    assert metadata_path.is_file()

    contract = yaml.safe_load(contract_path.read_text())
    metadata = yaml.safe_load(metadata_path.read_text())

    assert isinstance(contract, dict)
    assert isinstance(metadata, dict)
    assert metadata.get("deprecated") is not True
    assert contract.get("name") or contract.get("node_name") or contract.get("id")

    routing = contract.get("handler_routing")
    assert isinstance(routing, dict), f"{node_name} must declare handler_routing"


@pytest.mark.unit
@pytest.mark.parametrize("node_name", OMN_13530_TOUCHED_NODES)
def test_operation_match_handlers_declare_operation(node_name: str) -> None:
    """The exact OMN-13530 regression guard: every operation_match (or absent-strategy)
    handler entry must declare a non-empty ``operation`` so RuntimeLocal does not fail
    closed at startup."""
    contract = yaml.safe_load((NODES_ROOT / node_name / "contract.yaml").read_text())
    routing = contract["handler_routing"]
    strategy = routing.get("routing_strategy", "")
    if strategy not in ("", "operation_match"):
        pytest.skip(
            f"{node_name} routes by {strategy}; operation_match operation not required"
        )
    handlers = routing.get("handlers") or []
    assert handlers, f"{node_name} declares no handlers"
    for index, handler in enumerate(handlers):
        operation = handler.get("operation")
        missing_msg = (
            f"{node_name} handlers[{index}] is missing a non-empty 'operation' — "
            f"RuntimeLocal would fail closed with 'handlers[{index}].operation is missing'"
        )
        assert isinstance(operation, str), missing_msg
        assert operation.strip(), missing_msg
