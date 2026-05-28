from __future__ import annotations

from collections.abc import Mapping

import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.nodes.evidence_pipeline_native import (
    InMemoryDeploymentEvidenceProjectionStore,
)
from omnimarket.nodes.node_contract_matcher_compute import HandlerContractMatcherCompute
from omnimarket.nodes.node_deployment_evidence_reducer import (
    HandlerDeploymentEvidenceReducer,
)
from omnimarket.nodes.node_evidence_collector_effect import (
    HandlerEvidenceCollectorEffect,
)
from omnimarket.nodes.node_evidence_extractor_compute import (
    HandlerEvidenceExtractorCompute,
)
from omnimarket.nodes.node_evidence_pipeline_orchestrator import (
    HandlerEvidencePipelineOrchestrator,
)
from omnimarket.nodes.node_evidence_publisher_effect import (
    HandlerEvidencePublisherEffect,
)
from omnimarket.nodes.node_gap_analyzer_compute import HandlerGapAnalyzerCompute
from omnimarket.nodes.node_linear_updater_effect import HandlerLinearUpdaterEffect
from omnimarket.nodes.node_occ_pr_writer_effect import HandlerOccPrWriterEffect
from omnimarket.nodes.node_readiness_gate_orchestrator import (
    HandlerReadinessGateOrchestrator,
)
from omnimarket.nodes.node_readiness_scorer_compute import (
    HandlerReadinessScorerCompute,
)

SCOPED_NODES = (
    "node_deployment_evidence_reducer",
    "node_evidence_collector_effect",
    "node_evidence_extractor_compute",
    "node_evidence_pipeline_orchestrator",
    "node_evidence_publisher_effect",
    "node_contract_matcher_compute",
    "node_gap_analyzer_compute",
    "node_linear_updater_effect",
    "node_occ_pr_writer_effect",
    "node_readiness_gate_orchestrator",
    "node_readiness_scorer_compute",
)


def _command() -> ModelEvidencePipelineCommand:
    return ModelEvidencePipelineCommand(
        correlation_id="corr-omn-12395",
        validation_run_id="run-omn-12395",
        ticket_id="OMN-12395",
        repository="omnimarket",
        source_commit_sha="abcdef1234567890",
        requested_at="2026-05-28T20:00:00Z",
        trigger_surface="manual",
        source_pr=12395,
        deployment_id="deploy-omn-12395",
        topology_affecting=True,
        metadata={
            "deployment_id": "deploy-omn-12395",
            "changed_files": "src/omnimarket/nodes/evidence_pipeline_native.py,tests/test_evidence_pipeline_native_nodes.py",
            "source_surfaces": "git,ci,projection",
            "diff_refs": "git:abcdef1234567890",
            "test_output_refs": "pytest:tests/test_evidence_pipeline_native_nodes.py",
            "raw_payload_refs": "projection:deployment-readiness",
            "source_ci_run": "ci-12395",
            "contract_hash": "sha256:contract-omn-12395",
            "dod_items": "typed_handlers,golden_chain,node_scan",
            "satisfied_dod_items": "typed_handlers,golden_chain,node_scan",
            "expected_scope": "src,tests",
            "topology_affecting": "true",
            "collected_at": "2026-05-28T20:00:01Z",
        },
    )


def test_scoped_contracts_are_no_longer_node_not_implemented() -> None:
    for node_name in SCOPED_NODES:
        contract_path = "src/omnimarket/nodes/" + node_name + "/contract.yaml"
        with open(contract_path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        assert raw["node_not_implemented"] is False
        assert raw["handler"]["module"].startswith("omnimarket.nodes.")


def test_native_evidence_readiness_chain_is_deterministic() -> None:
    command = _command()
    raw = HandlerEvidenceCollectorEffect().handle(command)
    bundle = HandlerEvidenceExtractorCompute().handle(raw)
    validation = HandlerContractMatcherCompute().handle(bundle)
    occ_pr = HandlerOccPrWriterEffect().handle(validation)
    updated = HandlerLinearUpdaterEffect().handle(validation)
    published = HandlerEvidencePublisherEffect().handle(updated)
    gap_report = HandlerGapAnalyzerCompute().handle(validation)
    readiness = HandlerReadinessScorerCompute().handle(gap_report)
    gated = HandlerReadinessGateOrchestrator().handle(gap_report)

    store = InMemoryDeploymentEvidenceProjectionStore()
    reduced = HandlerDeploymentEvidenceReducer(store=store).handle(gated)

    assert raw.source_surfaces == ("git", "ci", "projection")
    assert bundle.evidence_bundle_hash.startswith("sha256:")
    assert validation.validation_state == "PASSED"
    assert validation.evidence_lifecycle_state == "VALIDATED"
    assert occ_pr.ticket_id == "OMN-12395"
    assert "linear:linear_update=acknowledged" in updated.evidence_refs
    assert published.validation_run_id == "run-omn-12395"
    assert gap_report.gap_classifications == {}
    assert readiness.readiness_state == "READY"
    assert gated.readiness_state == "READY"
    assert reduced.readiness_state == "READY"
    assert store.rows["deploy-omn-12395"]["readiness_state"] == "READY"


def test_gap_and_gate_block_missing_evidence() -> None:
    command = _command().model_copy(
        update={
            "metadata": {
                **dict(_command().metadata),
                "satisfied_dod_items": "typed_handlers",
            }
        }
    )
    raw = HandlerEvidenceCollectorEffect().handle(command)
    bundle = HandlerEvidenceExtractorCompute().handle(raw)
    validation = HandlerContractMatcherCompute().handle(bundle)
    gap_report = HandlerGapAnalyzerCompute().handle(validation)
    readiness = HandlerReadinessScorerCompute().handle(gap_report)

    assert validation.validation_state == "FAILED"
    assert set(validation.missing_dod_items) == {"golden_chain", "node_scan"}
    assert set(gap_report.gap_classifications.values()) == {
        "MISSING",
        "VALIDATION_FAILED",
    }
    assert readiness.readiness_state == "BLOCKED"
    assert readiness.blocking_reason_codes


class _RecordingPorts:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.collector = HandlerEvidenceCollectorEffect()
        self.extractor = HandlerEvidenceExtractorCompute()
        self.matcher = HandlerContractMatcherCompute()
        self.occ = HandlerOccPrWriterEffect()
        self.linear = HandlerLinearUpdaterEffect()
        self.publisher = HandlerEvidencePublisherEffect()

    def collect(self, command: ModelEvidencePipelineCommand):
        self.calls.append("collect")
        return self.collector.handle(command)

    def extract(self, raw):
        self.calls.append("extract")
        return self.extractor.handle(raw)

    def match_contract(self, bundle):
        self.calls.append("match_contract")
        return self.matcher.handle(bundle)

    def write_occ_pr(self, validation: ModelEvidenceValidationResult):
        self.calls.append("write_occ_pr")
        return self.occ.handle(validation)

    def update_linear(self, validation: ModelEvidenceValidationResult):
        self.calls.append("update_linear")
        return self.linear.handle(validation)

    def publish(self, event):
        self.calls.append("publish")
        return self.publisher.handle(event)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[Mapping[str, object]] = []

    def publish(self, event):
        self.events.append(event.model_dump(mode="json"))
        return {"published": "true"}


def test_orchestrator_uses_typed_ports_for_local_chain() -> None:
    ports = _RecordingPorts()
    occ_ref = HandlerEvidencePipelineOrchestrator(ports=ports).handle(_command())

    assert occ_ref.pr_url.endswith(f"/pull/{occ_ref.pr_number}")
    assert ports.calls == [
        "collect",
        "extract",
        "match_contract",
        "write_occ_pr",
        "update_linear",
        "publish",
        "publish",
    ]


def test_effect_nodes_use_injected_adapters() -> None:
    publisher = _RecordingPublisher()
    command = _command()
    validation = HandlerContractMatcherCompute().handle(
        HandlerEvidenceExtractorCompute().handle(
            HandlerEvidenceCollectorEffect().handle(command)
        )
    )

    result = HandlerEvidencePublisherEffect(adapter=publisher).handle(validation)

    assert result.validation_run_id == command.validation_run_id
    assert publisher.events[0]["validation_run_id"] == command.validation_run_id
