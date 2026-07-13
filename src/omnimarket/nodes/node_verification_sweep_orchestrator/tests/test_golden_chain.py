# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain tests for node_verification_sweep_orchestrator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
    HandlerVerificationSweepOrchestrator,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
    ModelVerificationSweepOrchestratorRequest,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
    ModelDatabaseVerificationResult,
    ModelDodEvidenceVerificationResult,
    ModelEndpointVerificationResult,
)


@pytest.fixture
def node_dir() -> Path:
    """Path to the node_verification_sweep_orchestrator directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


class FakeProbeAdapter:
    def __init__(
        self,
        *,
        endpoint_status: str = "PASS",
        db_status: str = "PASS",
        dod_status: str = "PASS",
        fail_phase: str = "",
    ) -> None:
        self.endpoint_status = endpoint_status
        self.db_status = db_status
        self.dod_status = dod_status
        self.fail_phase = fail_phase
        self.calls: list[tuple[str, str]] = []

    def resolve_targets(
        self, request: ModelVerificationSweepOrchestratorRequest
    ) -> Sequence[str]:
        self.calls.append(("resolve", request.epic or request.pr or ""))
        return ("OMN-12345",)

    def verify_dashboard(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelEndpointVerificationResult | Mapping[str, Any]]:
        self.calls.append(("dashboard", target))
        if self.fail_phase == "dashboard":
            raise RuntimeError("dashboard unavailable")
        return (
            {
                "endpoint": f"https://example.test/{target}",
                "status": self.endpoint_status,
                "http_code": 200 if self.endpoint_status == "PASS" else 500,
                "evidence": "endpoint returned expected data",
            },
        )

    def verify_database(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelDatabaseVerificationResult | Mapping[str, Any]]:
        self.calls.append(("database", target))
        if self.fail_phase == "database":
            raise RuntimeError("database unavailable")
        return (
            {
                "table": f"projection_{target.lower().replace('-', '_')}",
                "status": self.db_status,
                "row_count": 4 if self.db_status == "PASS" else 0,
                "evidence": "projection table has expected rows",
            },
        )

    def verify_dod_evidence(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelDodEvidenceVerificationResult | Mapping[str, Any]]:
        self.calls.append(("dod_evidence", target))
        if self.fail_phase == "dod_evidence":
            raise RuntimeError("dod evidence unavailable")
        return (
            {
                "evidence_type": "rendered_output",
                "status": self.dod_status,
                "evidence": "passing rendered_output receipt found",
            },
        )


class FakeReceiptWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[Mapping[str, Any]] = []

    def write_receipt(self, payload: Mapping[str, Any]) -> str:
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("receipt store unavailable")
        return "/tmp/verification-sweep/OMN-12345.yaml"


class FakeLinearCommenter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.comments: list[tuple[str, str]] = []

    def post_verification_comment(self, ticket_id: str, body: str) -> str:
        self.comments.append((ticket_id, body))
        if self.fail:
            raise RuntimeError("linear unavailable")
        return "comment-1"


class TestContractYaml:
    def test_contract_is_functional_orchestrator(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        assert data["name"] == "node_verification_sweep_orchestrator"
        assert data["node_type"] == "orchestrator"
        assert data["node_not_implemented"] is False
        assert data["handler"]["class"] == "HandlerVerificationSweepOrchestrator"

    def test_contract_declares_native_adapter_boundaries(
        self, contract_path: Path
    ) -> None:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        adapter_names = {item["name"] for item in data["dependencies"]["adapters"]}
        assert "ProtocolVerificationProbeAdapter" in adapter_names
        assert "ProtocolVerificationReceiptWriter" in adapter_names
        assert "ProtocolVerificationLinearCommenter" in adapter_names


class TestHandlerGoldenChain:
    def test_successful_verification_writes_receipt(self) -> None:
        probe = FakeProbeAdapter()
        receipt_writer = FakeReceiptWriter()
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=probe,
            receipt_writer=receipt_writer,
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(targets=("OMN-12345",))
        )

        assert result.overall_status == "pass"
        assert result.receipt_path == "/tmp/verification-sweep/OMN-12345.yaml"
        assert result.adapter_errors == []
        assert len(result.endpoint_results) == 1
        assert len(result.db_checks) == 1
        assert len(result.dod_receipts) == 1
        assert len(receipt_writer.payloads) == 1

    def test_failed_verification_comments_and_writes_receipt(self) -> None:
        probe = FakeProbeAdapter(db_status="FAIL_EMPTY")
        receipt_writer = FakeReceiptWriter()
        linear = FakeLinearCommenter()
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=probe,
            receipt_writer=receipt_writer,
            linear_commenter=linear,
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(targets=("OMN-12345",))
        )

        assert result.overall_status == "fail"
        assert result.db_checks[0].status == "FAIL_EMPTY"
        assert result.receipt_path == "/tmp/verification-sweep/OMN-12345.yaml"
        assert len(receipt_writer.payloads) == 1
        assert linear.comments == [
            (
                "OMN-12345",
                "Verification sweep completed with overall_status=fail, "
                "endpoint_failures=0, database_failures=1, dod_failures=0, "
                "adapter_errors=0.",
            )
        ]

    def test_dry_run_has_no_receipt_or_linear_side_effects(self) -> None:
        probe = FakeProbeAdapter(endpoint_status="FAIL_HTTP")
        receipt_writer = FakeReceiptWriter()
        linear = FakeLinearCommenter()
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=probe,
            receipt_writer=receipt_writer,
            linear_commenter=linear,
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(
                targets=("OMN-12345",),
                dry_run=True,
            )
        )

        assert result.overall_status == "fail"
        assert result.receipt_path == ""
        assert result.dry_run is True
        assert receipt_writer.payloads == []
        assert linear.comments == []

    def test_probe_adapter_error_is_typed_failure(self) -> None:
        probe = FakeProbeAdapter(fail_phase="dashboard")
        receipt_writer = FakeReceiptWriter()
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=probe,
            receipt_writer=receipt_writer,
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(
                targets=("OMN-12345",),
                check_types=("dashboard",),
            )
        )

        assert result.overall_status == "fail"
        assert result.endpoint_results[0].status == "FAIL_HTTP"
        assert result.adapter_errors[0].phase == "dashboard"
        assert result.adapter_errors[0].target == "OMN-12345"
        assert "dashboard unavailable" in result.adapter_errors[0].error
        assert len(receipt_writer.payloads) == 1

    def test_receipt_adapter_error_is_typed_failure(self) -> None:
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=FakeProbeAdapter(),
            receipt_writer=FakeReceiptWriter(fail=True),
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(targets=("OMN-12345",))
        )

        assert result.overall_status == "fail"
        assert result.receipt_path == ""
        assert result.adapter_errors[0].phase == "receipt_write"
        assert "receipt store unavailable" in result.adapter_errors[0].error

    def test_epic_target_resolution_uses_probe_adapter(self) -> None:
        probe = FakeProbeAdapter()
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=probe,
            receipt_writer=FakeReceiptWriter(),
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(epic="OMN-EPIC")
        )

        assert result.overall_status == "pass"
        assert ("resolve", "OMN-EPIC") in probe.calls


class TestEmptyVerificationSetFailsClosed:
    """OMN-14552 — a sweep that verifies nothing must FAIL, never green over ∅.

    RED-against-exists-but-wrong: the *empty verification set* is a scope that
    genuinely exists (a request with no targets/epic/pr, or an epic that
    resolves to zero children) but is the WRONG scope to certify anything
    against. Pre-fix the handler returned ``overall_status="skip"`` — a
    non-``fail`` verdict a caller treats as "not a failure" = passing. This is
    the exact "green over nothing" disease of OMN-14531. Fail-closed: refuse to
    report any non-``fail`` verdict over ``scanned_count == 0``.
    """

    def test_no_targets_refuses_pass(self) -> None:
        # No targets, no epic, no pr → zero-size verification set.
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=FakeProbeAdapter(),
            receipt_writer=FakeReceiptWriter(),
        )

        result = handler.handle(ModelVerificationSweepOrchestratorRequest())

        assert result.overall_status == "fail"
        assert result.scanned_count == 0
        assert any(e.phase == "empty_scope" for e in result.adapter_errors)

    def test_epic_resolving_to_zero_children_refuses_pass(self) -> None:
        # The epic exists but resolves to no children → still an empty set.
        class _EmptyResolveProbe(FakeProbeAdapter):
            def resolve_targets(
                self, _request: ModelVerificationSweepOrchestratorRequest
            ) -> Sequence[str]:
                return ()

        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=_EmptyResolveProbe(),
            receipt_writer=FakeReceiptWriter(),
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(epic="OMN-EMPTY-EPIC")
        )

        assert result.overall_status == "fail"
        assert result.scanned_count == 0
        assert any(e.phase == "empty_scope" for e in result.adapter_errors)

    def test_dry_run_empty_set_still_fails_closed(self) -> None:
        # Fail-closed applies even in dry_run — a plan over ∅ is still ∅.
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=FakeProbeAdapter(),
            receipt_writer=FakeReceiptWriter(),
        )

        result = handler.handle(ModelVerificationSweepOrchestratorRequest(dry_run=True))

        assert result.overall_status == "fail"
        assert result.scanned_count == 0
        assert result.receipt_path == ""

    def test_genuinely_verified_set_passes_with_positive_scanned_count(self) -> None:
        # GREEN only against a real, non-empty, all-PASS verification set.
        handler = HandlerVerificationSweepOrchestrator(
            probe_adapter=FakeProbeAdapter(),
            receipt_writer=FakeReceiptWriter(),
        )

        result = handler.handle(
            ModelVerificationSweepOrchestratorRequest(targets=("OMN-12345",))
        )

        assert result.overall_status == "pass"
        assert result.scanned_count == 1
        assert not any(e.phase == "empty_scope" for e in result.adapter_errors)

    def test_correlation_id_from_envelope_payload_is_accepted(self) -> None:
        # Dispatch seam: the runtime injects correlation_id into the envelope
        # payload dict; an extra="forbid" model without the field rejected it
        # with extra_forbidden BEFORE handle() ran (integration_sweep OMN-13145).
        from uuid import uuid4

        request = ModelVerificationSweepOrchestratorRequest(
            correlation_id=uuid4(),
            targets=("OMN-12345",),
        )

        assert request.correlation_id is not None
