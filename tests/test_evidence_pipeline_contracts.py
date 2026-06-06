from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

EXPECTED_NODES = {
    "node_evidence_pipeline_orchestrator": {
        "node_type": "ORCHESTRATOR_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-pipeline-completed.v1",
        "subscribe": {"onex.cmd.omnimarket.evidence-pipeline-start.v1"},
        "publish": {"onex.evt.omnimarket.evidence-pipeline-completed.v1"},
    },
    "node_readiness_gate_orchestrator": {
        "node_type": "ORCHESTRATOR_GENERIC",
        "terminal_event": "onex.evt.omnimarket.readiness-gate-completed.v1",
        "subscribe": {
            "onex.cmd.omnimarket.readiness-gate-start.v1",
            "onex.evt.omnimarket.readiness-scored.v1",
        },
        "publish": {
            "onex.evt.omnimarket.readiness-gate-completed.v1",
            "onex.evt.omnimarket.readiness-gate-blocked.v1",
        },
    },
    "node_evidence_extractor_compute": {
        "node_type": "COMPUTE_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-extracted.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-collected.v1"},
        "publish": {"onex.evt.omnimarket.evidence-extracted.v1"},
    },
    "node_contract_matcher_compute": {
        "node_type": "COMPUTE_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-validated.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-extracted.v1"},
        "publish": {"onex.evt.omnimarket.evidence-validated.v1"},
    },
    "node_gap_analyzer_compute": {
        "node_type": "COMPUTE_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-gap-analyzed.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-validated.v1"},
        "publish": {"onex.evt.omnimarket.evidence-gap-analyzed.v1"},
    },
    "node_readiness_scorer_compute": {
        "node_type": "COMPUTE_GENERIC",
        "terminal_event": "onex.evt.omnimarket.readiness-scored.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-gap-analyzed.v1"},
        "publish": {"onex.evt.omnimarket.readiness-scored.v1"},
    },
    "node_evidence_collector_effect": {
        "node_type": "EFFECT_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-collected.v1",
        "subscribe": {"onex.cmd.omnimarket.evidence-pipeline-start.v1"},
        "publish": {"onex.evt.omnimarket.evidence-collected.v1"},
    },
    "node_occ_pr_writer_effect": {
        "node_type": "EFFECT_GENERIC",
        "terminal_event": "onex.evt.omnimarket.occ-pr-created.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-validated.v1"},
        "publish": {"onex.evt.omnimarket.occ-pr-created.v1"},
    },
    "node_linear_updater_effect": {
        "node_type": "EFFECT_GENERIC",
        "terminal_event": "onex.evt.omnimarket.linear-evidence-updated.v1",
        "subscribe": {"onex.evt.omnimarket.evidence-validated.v1"},
        "publish": {"onex.evt.omnimarket.linear-evidence-updated.v1"},
    },
    "node_evidence_publisher_effect": {
        "node_type": "EFFECT_GENERIC",
        "terminal_event": "onex.evt.omnimarket.evidence-published.v1",
        "subscribe": {
            "onex.evt.omnimarket.evidence-validated.v1",
            "onex.evt.omnimarket.occ-pr-created.v1",
            "onex.evt.omnimarket.readiness-scored.v1",
        },
        "publish": {"onex.evt.omnimarket.evidence-published.v1"},
    },
    "node_deployment_evidence_reducer": {
        "node_type": "REDUCER_GENERIC",
        "terminal_event": "onex.evt.omnimarket.deployment-evidence-reduced.v1",
        "subscribe": {
            "onex.evt.omnimarket.evidence-validated.v1",
            "onex.evt.omnimarket.readiness-scored.v1",
            "onex.evt.omnimarket.occ-pr-created.v1",
        },
        "publish": {"onex.evt.omnimarket.deployment-evidence-reduced.v1"},
    },
}

COMPAT_MODEL_PREFIX = "omnibase_compat.contracts.evidence_pipeline.wire."


def _load_contract(node_name: str) -> dict[str, Any]:
    contract_path = NODES_ROOT / node_name / "contract.yaml"
    with contract_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def test_evidence_pipeline_nodes_are_entrypoint_packages() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]

    for node_name in EXPECTED_NODES:
        node_dir = NODES_ROOT / node_name
        assert node_dir.is_dir()
        assert (node_dir / "__init__.py").is_file()
        assert (node_dir / "metadata.yaml").is_file()
        assert entry_points[node_name] == f"omnimarket.nodes.{node_name}"


def test_evidence_pipeline_contract_topics_and_terminal_events() -> None:
    for node_name, expected in EXPECTED_NODES.items():
        contract = _load_contract(node_name)
        event_bus = contract["event_bus"]

        assert contract["name"] == node_name
        assert contract["node_type"] == expected["node_type"]
        assert contract["terminal_event"] == expected["terminal_event"]
        assert set(event_bus["subscribe_topics"]) == expected["subscribe"]
        assert set(event_bus["publish_topics"]) == expected["publish"]


def test_evidence_pipeline_contracts_are_native_implemented_nodes() -> None:
    for node_name in EXPECTED_NODES:
        contract = _load_contract(node_name)
        metadata = contract["metadata"]

        assert "handler" in contract
        assert "handler_routing" in contract
        assert contract["node_not_implemented"] is False
        assert metadata["implementation_wave"] == 3
        assert "handlers_deferred_until_wave" not in metadata
        assert "OMN-12395" in metadata["related_tickets"]


def test_evidence_pipeline_contracts_use_wave_1_wire_models() -> None:
    for node_name in EXPECTED_NODES:
        contract = _load_contract(node_name)
        for key in ("input_model", "output_model"):
            model = contract[key]
            assert model["module"].startswith(COMPAT_MODEL_PREFIX), node_name


def test_evidence_pipeline_authority_invariants_are_declared() -> None:
    pipeline = _load_contract("node_evidence_pipeline_orchestrator")
    readiness = _load_contract("node_readiness_gate_orchestrator")
    matcher = _load_contract("node_contract_matcher_compute")
    gap_analyzer = _load_contract("node_gap_analyzer_compute")
    readiness_scorer = _load_contract("node_readiness_scorer_compute")
    reducer = _load_contract("node_deployment_evidence_reducer")

    assert pipeline["evidence_authority"]["provisional_is_completion_proof"] is False
    assert readiness["evidence_authority"]["authoritative_for_deploy"] is True
    assert readiness["evidence_authority"]["requires_finalized_evidence"] is True
    assert matcher["validation_contract"]["llm_allowed"] is False
    assert matcher["replay"]["timestamp_authority"] == "ingest_sequence"
    assert gap_analyzer["replay"]["deterministic"] is True
    assert readiness_scorer["replay"]["requires_validator_version"] is True
    assert reducer["reducer_contract"]["append_only"] is True
    assert reducer["reducer_contract"]["ordering_authority"] == "ingest_sequence"
