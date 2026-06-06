# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerVerificationSweepOrchestrator — post-orchestration verification sweep.

Probes dashboard endpoints, checks database tables, and validates dod_evidence
rendered_output items. Non-blocking — writes receipts and optional Linear
comments but does not halt orchestration.

ONEX node type: ORCHESTRATOR — impure (network I/O, filesystem writes).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
    ModelVerificationSweepOrchestratorRequest,
    VerificationCheckType,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
    AdapterErrorPhase,
    ModelDatabaseVerificationResult,
    ModelDodEvidenceVerificationResult,
    ModelEndpointVerificationResult,
    ModelVerificationAdapterError,
    ModelVerificationSweepOrchestratorResult,
    VerificationStatus,
)

_ALL_CHECK_TYPES: tuple[VerificationCheckType, ...] = (
    "dashboard",
    "database",
    "dod_evidence",
)


class ProtocolVerificationProbeAdapter(Protocol):
    """Adapter boundary for authoritative verification surfaces."""

    def resolve_targets(
        self, request: ModelVerificationSweepOrchestratorRequest
    ) -> Sequence[str]: ...

    def verify_dashboard(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelEndpointVerificationResult | Mapping[str, Any]]: ...

    def verify_database(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelDatabaseVerificationResult | Mapping[str, Any]]: ...

    def verify_dod_evidence(
        self, target: str, *, timeout_seconds: int
    ) -> Sequence[ModelDodEvidenceVerificationResult | Mapping[str, Any]]: ...


class ProtocolVerificationReceiptWriter(Protocol):
    """Adapter boundary for durable OCC verification receipt writes."""

    def write_receipt(self, payload: Mapping[str, Any]) -> str: ...


class ProtocolVerificationLinearCommenter(Protocol):
    """Adapter boundary for optional Linear annotations."""

    def post_verification_comment(self, ticket_id: str, body: str) -> str: ...


class HandlerVerificationSweepOrchestrator:
    """Orchestrate post-orchestration verification via injected native adapters."""

    def __init__(
        self,
        *,
        probe_adapter: ProtocolVerificationProbeAdapter | None = None,
        receipt_writer: ProtocolVerificationReceiptWriter | None = None,
        linear_commenter: ProtocolVerificationLinearCommenter | None = None,
    ) -> None:
        self._probe_adapter = probe_adapter
        self._receipt_writer = receipt_writer
        self._linear_commenter = linear_commenter

    def handle(
        self,
        request: ModelVerificationSweepOrchestratorRequest,
    ) -> ModelVerificationSweepOrchestratorResult:
        """Run verification sweep across dashboard endpoints, database tables, and DoD evidence."""
        check_types = request.check_types or _ALL_CHECK_TYPES
        endpoint_results: list[ModelEndpointVerificationResult] = []
        db_checks: list[ModelDatabaseVerificationResult] = []
        dod_receipts: list[ModelDodEvidenceVerificationResult] = []
        adapter_errors: list[ModelVerificationAdapterError] = []

        targets = self._resolve_targets(request, adapter_errors)
        for target in targets:
            if "dashboard" in check_types:
                endpoint_results.extend(
                    self._verify_dashboard(target, request, adapter_errors)
                )
            if "database" in check_types:
                db_checks.extend(self._verify_database(target, request, adapter_errors))
            if "dod_evidence" in check_types:
                dod_receipts.extend(
                    self._verify_dod_evidence(target, request, adapter_errors)
                )

        overall_status = _overall_status(
            endpoint_results=endpoint_results,
            db_checks=db_checks,
            dod_receipts=dod_receipts,
            adapter_errors=adapter_errors,
        )
        result = ModelVerificationSweepOrchestratorResult(
            endpoint_results=endpoint_results,
            db_checks=db_checks,
            dod_receipts=dod_receipts,
            overall_status=overall_status,
            dry_run=request.dry_run,
            adapter_errors=adapter_errors,
        )

        if request.dry_run:
            return result

        receipt_path = self._write_receipt(result, request, targets, adapter_errors)
        if adapter_errors:
            overall_status = _overall_status(
                endpoint_results=endpoint_results,
                db_checks=db_checks,
                dod_receipts=dod_receipts,
                adapter_errors=adapter_errors,
            )
            result = result.model_copy(
                update={
                    "overall_status": overall_status,
                    "receipt_path": receipt_path,
                    "adapter_errors": adapter_errors,
                }
            )
        else:
            result = result.model_copy(update={"receipt_path": receipt_path})

        if result.overall_status in {"fail", "partial"} and self._linear_commenter:
            self._post_linear_comments(result, targets, adapter_errors)
            if adapter_errors:
                result = result.model_copy(
                    update={
                        "overall_status": _overall_status(
                            endpoint_results=endpoint_results,
                            db_checks=db_checks,
                            dod_receipts=dod_receipts,
                            adapter_errors=adapter_errors,
                        ),
                        "adapter_errors": adapter_errors,
                    }
                )
        return result

    def _resolve_targets(
        self,
        request: ModelVerificationSweepOrchestratorRequest,
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> tuple[str, ...]:
        explicit_targets = tuple(
            target.strip() for target in request.targets if target.strip()
        )
        if explicit_targets:
            return explicit_targets
        if not request.epic and not request.pr:
            return ()
        adapter = self._require_probe_adapter()
        try:
            return tuple(
                target.strip()
                for target in adapter.resolve_targets(request)
                if target.strip()
            )
        except Exception as exc:
            adapter_errors.append(
                _adapter_error(
                    phase="target_resolution",
                    adapter=adapter,
                    error=exc,
                )
            )
            return ()

    def _verify_dashboard(
        self,
        target: str,
        request: ModelVerificationSweepOrchestratorRequest,
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> list[ModelEndpointVerificationResult]:
        adapter = self._require_probe_adapter()
        try:
            return [
                _coerce_endpoint_result(item)
                for item in adapter.verify_dashboard(
                    target,
                    timeout_seconds=request.timeout_seconds,
                )
            ]
        except Exception as exc:
            adapter_errors.append(
                _adapter_error("dashboard", adapter=adapter, error=exc, target=target)
            )
            return [
                ModelEndpointVerificationResult(
                    endpoint=target,
                    status="FAIL_HTTP",
                    evidence=f"dashboard adapter error: {exc}",
                )
            ]

    def _verify_database(
        self,
        target: str,
        request: ModelVerificationSweepOrchestratorRequest,
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> list[ModelDatabaseVerificationResult]:
        adapter = self._require_probe_adapter()
        try:
            return [
                _coerce_database_result(item)
                for item in adapter.verify_database(
                    target,
                    timeout_seconds=request.timeout_seconds,
                )
            ]
        except Exception as exc:
            adapter_errors.append(
                _adapter_error("database", adapter=adapter, error=exc, target=target)
            )
            return [
                ModelDatabaseVerificationResult(
                    table=target,
                    status="FAIL_SCHEMA",
                    evidence=f"database adapter error: {exc}",
                )
            ]

    def _verify_dod_evidence(
        self,
        target: str,
        request: ModelVerificationSweepOrchestratorRequest,
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> list[ModelDodEvidenceVerificationResult]:
        adapter = self._require_probe_adapter()
        try:
            return [
                _coerce_dod_result(item)
                for item in adapter.verify_dod_evidence(
                    target,
                    timeout_seconds=request.timeout_seconds,
                )
            ]
        except Exception as exc:
            adapter_errors.append(
                _adapter_error(
                    "dod_evidence", adapter=adapter, error=exc, target=target
                )
            )
            return [
                ModelDodEvidenceVerificationResult(
                    evidence_type=target,
                    status="FAIL_NO_RECEIPT",
                    evidence=f"dod_evidence adapter error: {exc}",
                )
            ]

    def _write_receipt(
        self,
        result: ModelVerificationSweepOrchestratorResult,
        request: ModelVerificationSweepOrchestratorRequest,
        targets: tuple[str, ...],
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> str:
        if self._receipt_writer is None:
            adapter_errors.append(
                ModelVerificationAdapterError(
                    phase="receipt_write",
                    adapter="ProtocolVerificationReceiptWriter",
                    error="receipt_writer adapter required when dry_run is false",
                )
            )
            return ""
        payload = {
            "request": request.model_dump(mode="json"),
            "targets": list(targets),
            "result": result.model_dump(mode="json"),
        }
        try:
            return self._receipt_writer.write_receipt(payload)
        except Exception as exc:
            adapter_errors.append(
                _adapter_error("receipt_write", adapter=self._receipt_writer, error=exc)
            )
            return ""

    def _post_linear_comments(
        self,
        result: ModelVerificationSweepOrchestratorResult,
        targets: tuple[str, ...],
        adapter_errors: list[ModelVerificationAdapterError],
    ) -> None:
        if self._linear_commenter is None:
            return
        body = _linear_comment_body(result)
        for target in targets:
            try:
                self._linear_commenter.post_verification_comment(target, body)
            except Exception as exc:
                adapter_errors.append(
                    _adapter_error(
                        "linear_comment",
                        adapter=self._linear_commenter,
                        error=exc,
                        target=target,
                    )
                )

    def _require_probe_adapter(self) -> ProtocolVerificationProbeAdapter:
        if self._probe_adapter is None:
            raise RuntimeError("verification probe adapter required")
        return self._probe_adapter


def _coerce_endpoint_result(
    item: ModelEndpointVerificationResult | Mapping[str, Any],
) -> ModelEndpointVerificationResult:
    if isinstance(item, ModelEndpointVerificationResult):
        return item
    return ModelEndpointVerificationResult.model_validate(item)


def _coerce_database_result(
    item: ModelDatabaseVerificationResult | Mapping[str, Any],
) -> ModelDatabaseVerificationResult:
    if isinstance(item, ModelDatabaseVerificationResult):
        return item
    return ModelDatabaseVerificationResult.model_validate(item)


def _coerce_dod_result(
    item: ModelDodEvidenceVerificationResult | Mapping[str, Any],
) -> ModelDodEvidenceVerificationResult:
    if isinstance(item, ModelDodEvidenceVerificationResult):
        return item
    return ModelDodEvidenceVerificationResult.model_validate(item)


def _overall_status(
    *,
    endpoint_results: Sequence[ModelEndpointVerificationResult],
    db_checks: Sequence[ModelDatabaseVerificationResult],
    dod_receipts: Sequence[ModelDodEvidenceVerificationResult],
    adapter_errors: Sequence[ModelVerificationAdapterError],
) -> VerificationStatus:
    statuses = [
        item.status
        for item in (list(endpoint_results) + list(db_checks) + list(dod_receipts))
    ]
    if adapter_errors or any(status.startswith("FAIL") for status in statuses):
        return "fail"
    if not statuses or all(status == "SKIP" for status in statuses):
        return "skip"
    if any(status == "SKIP" for status in statuses):
        return "partial"
    return "pass"


def _adapter_error(
    phase: AdapterErrorPhase,
    *,
    adapter: object,
    error: BaseException,
    target: str = "",
) -> ModelVerificationAdapterError:
    return ModelVerificationAdapterError(
        phase=phase,
        target=target,
        adapter=adapter.__class__.__name__,
        error=str(error),
    )


def _linear_comment_body(result: ModelVerificationSweepOrchestratorResult) -> str:
    return (
        "Verification sweep completed with "
        f"overall_status={result.overall_status}, "
        f"endpoint_failures={_count_failures(result.endpoint_results)}, "
        f"database_failures={_count_failures(result.db_checks)}, "
        f"dod_failures={_count_failures(result.dod_receipts)}, "
        f"adapter_errors={len(result.adapter_errors)}."
    )


def _count_failures(items: Sequence[Any]) -> int:
    return sum(
        1 for item in items if str(getattr(item, "status", "")).startswith("FAIL")
    )
