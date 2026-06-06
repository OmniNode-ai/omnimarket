"""Materialize reducer-owned deployment evidence projection state.

The reducer consumes append-only evidence/readiness/OCC events and projects
deployment evidence and readiness into Postgres-backed projection tables through
the canonical ``ProtocolProjectionDatabaseSync`` adapter. The readiness gate
consumes this reducer-owned projection state — not logs or workflow summaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_occ_pr_reference import (
    ModelOccPrReference,
)

from omnimarket.nodes.evidence_pipeline_native import (
    TypedEvidenceEvent,
    coerce_evidence_event,
    reduce_deployment_evidence,
)
from omnimarket.nodes.node_deployment_evidence_reducer.models.model_reduction_result import (
    ModelDeploymentEvidenceReductionResult,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

DEPLOYMENT_EVIDENCE_TABLE = "deployment_evidence_projection"
DEPLOYMENT_READINESS_TABLE = "deployment_readiness_projection"


class HandlerDeploymentEvidenceReducer:
    """Project append-only deployment evidence events into reducer-owned state."""

    def handle(
        self, input_data: TypedEvidenceEvent | Mapping[str, object]
    ) -> dict[str, object]:
        """Runtime entrypoint: projects through the injected ``_db`` adapter."""
        if not isinstance(input_data, Mapping):
            raise TypeError(
                "handle() requires a Mapping payload carrying the projection "
                "database in input_data['_db']"
            )
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = coerce_evidence_event(payload)
        return self.project(event, db_raw).model_dump(mode="json")

    def project(
        self,
        event: TypedEvidenceEvent | Mapping[str, object],
        db: DatabaseAdapter,
    ) -> ModelDeploymentEvidenceReductionResult:
        """Reduce one event into the reducer-owned projection tables."""
        typed_event = coerce_evidence_event(event)
        readiness = reduce_deployment_evidence(typed_event)

        rows = 0
        if db.upsert(
            DEPLOYMENT_EVIDENCE_TABLE,
            "deployment_id",
            _evidence_row(typed_event, readiness),
        ):
            rows += 1
        if db.upsert(
            DEPLOYMENT_READINESS_TABLE,
            "deployment_id",
            _readiness_row(readiness),
        ):
            rows += 1

        return ModelDeploymentEvidenceReductionResult(
            deployment_id=readiness.deployment_id,
            correlation_id=readiness.correlation_id,
            validation_run_id=readiness.validation_run_id,
            readiness_state=readiness.readiness_state,
            rows_upserted=rows,
            tables=(DEPLOYMENT_EVIDENCE_TABLE, DEPLOYMENT_READINESS_TABLE),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _join(values: tuple[str, ...]) -> str:
    return ",".join(values)


def _evidence_row(
    event: TypedEvidenceEvent,
    readiness: ModelDeploymentReadinessResult,
) -> dict[str, object]:
    row: dict[str, object] = {
        "deployment_id": readiness.deployment_id,
        "correlation_id": readiness.correlation_id,
        "ticket_id": readiness.deployment_id,
        "validation_run_id": readiness.validation_run_id,
        "repository": "",
        "evidence_lifecycle_state": "PROVISIONAL",
        "validation_state": "ADVISORY_ONLY",
        "readiness_state": readiness.readiness_state,
        "topology_affecting": readiness.topology_affecting,
        "blocking_reason_codes": _join(readiness.blocking_reason_codes),
        "contract_hash": "",
        "evidence_bundle_hash": "",
        "updated_at": _now(),
    }
    if isinstance(event, ModelEvidenceValidationResult):
        row.update(
            {
                "ticket_id": event.ticket_id,
                "repository": event.repository,
                "evidence_lifecycle_state": event.evidence_lifecycle_state,
                "validation_state": event.validation_state,
                "contract_hash": event.contract_hash,
                "evidence_bundle_hash": event.evidence_bundle_hash,
            }
        )
    elif isinstance(event, ModelOccPrReference):
        row.update(
            {
                "ticket_id": event.ticket_id,
                "repository": event.occ_repository,
                "evidence_lifecycle_state": event.evidence_lifecycle_state,
            }
        )
    return row


def _readiness_row(readiness: ModelDeploymentReadinessResult) -> dict[str, object]:
    return {
        "deployment_id": readiness.deployment_id,
        "correlation_id": readiness.correlation_id,
        "validation_run_id": readiness.validation_run_id,
        "readiness_state": readiness.readiness_state,
        "blocking_reason_codes": _join(readiness.blocking_reason_codes),
        "gap_report_hash": readiness.gap_report_hash,
        "validator_version": readiness.validator_version,
        "updated_at": _now(),
    }


__all__ = [
    "DEPLOYMENT_EVIDENCE_TABLE",
    "DEPLOYMENT_READINESS_TABLE",
    "HandlerDeploymentEvidenceReducer",
]
