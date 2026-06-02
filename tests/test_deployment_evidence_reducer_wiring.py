"""OMN-12578 Phase 3: deployment evidence reducer wiring proof.

These tests prove the reducer-owned deployment evidence projection is wired:

1. The contract subscribe topics derive the canonical Kafka consumer group id
   the runtime mints when auto-wiring the reducer (the group that is currently
   absent on every .201 lane per the OMN-12575 baseline).
2. A projection sink migration exists and its tables/columns match the
   reducer contract's declared ``projection_surfaces`` and ``projection_api``.
3. Deployment-evidence validation events update the reducer-owned projection
   tables through the canonical ``ProtocolProjectionDatabaseSync`` adapter.
4. Readiness is derived from reducer-owned projection state, not logs or
   workflow summaries.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnibase_infra.enums import EnumConsumerGroupPurpose
from omnibase_infra.models import ModelNodeIdentity
from omnibase_infra.utils import compute_consumer_group_id

from omnimarket.nodes.node_deployment_evidence_reducer import (
    HandlerDeploymentEvidenceReducer,
)
from omnimarket.nodes.node_deployment_evidence_reducer.handlers.handler_deployment_evidence_reducer import (
    DEPLOYMENT_EVIDENCE_TABLE,
    DEPLOYMENT_READINESS_TABLE,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
REDUCER_DIR = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_deployment_evidence_reducer"
)
MIGRATIONS_DIR = REDUCER_DIR / "migrations"


def _contract() -> dict[str, object]:
    with (REDUCER_DIR / "contract.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _passing_validation(
    *, deployment_id: str, run: str
) -> ModelEvidenceValidationResult:
    return ModelEvidenceValidationResult(
        correlation_id=f"corr-{run}",
        validation_run_id=run,
        ticket_id="OMN-12578",
        repository="omnimarket",
        contract_hash="sha256:contract-omn-12578",
        evidence_bundle_hash="sha256:bundle-omn-12578",
        verifier_identity="omnimarket.node_contract_matcher_compute",
        validator_version="evidence-readiness-native-v1",
        validated_at="2026-06-01T20:00:00Z",
        validation_state="PASSED",
        evidence_lifecycle_state="VALIDATED",
        topology_affecting=True,
        evidence_refs=(f"deployment:{deployment_id}",),
    )


def _failing_validation(
    *, deployment_id: str, run: str
) -> ModelEvidenceValidationResult:
    return ModelEvidenceValidationResult(
        correlation_id=f"corr-{run}",
        validation_run_id=run,
        ticket_id="OMN-12578",
        repository="omnimarket",
        contract_hash="sha256:contract-omn-12578",
        evidence_bundle_hash="sha256:bundle-omn-12578",
        verifier_identity="omnimarket.node_contract_matcher_compute",
        validator_version="evidence-readiness-native-v1",
        validated_at="2026-06-01T20:05:00Z",
        validation_state="FAILED",
        evidence_lifecycle_state="REJECTED",
        topology_affecting=True,
        blocking_reason_codes=("missing_dod_items",),
        missing_dod_items=("golden_chain",),
        evidence_refs=(f"deployment:{deployment_id}",),
    )


def test_reducer_consumer_group_matches_runtime_auto_wiring() -> None:
    """The reducer's contract topics derive the canonical CONSUME group id.

    This is the group the runtime auto-wiring mints; it is the surface that is
    absent on .201 today (OMN-12575 baseline). Pinning it makes the wiring
    target explicit and regression-guarded.
    """
    contract = _contract()
    subscribe = contract["event_bus"]["subscribe_topics"]  # type: ignore[index]
    assert "onex.evt.omnimarket.evidence-validated.v1" in subscribe

    identity = ModelNodeIdentity(
        env="stability-test",
        service="omnimarket",
        node_name="node_deployment_evidence_reducer",
        version="1.0.0",
    )
    group = compute_consumer_group_id(identity, EnumConsumerGroupPurpose.CONSUME)
    assert (
        group
        == "stability-test.omnimarket.node_deployment_evidence_reducer.consume.1.0.0"
    )


def test_projection_sink_migration_exists_and_matches_contract() -> None:
    """The reducer ships a migration whose tables match its projection surfaces."""
    assert MIGRATIONS_DIR.is_dir(), "reducer must own a migrations/ directory"
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert sql_files, "reducer migration SQL must exist"
    sql = "\n".join(path.read_text(encoding="utf-8") for path in sql_files)

    contract = _contract()
    surfaces = contract["reducer_contract"]["projection_surfaces"]  # type: ignore[index]
    assert set(surfaces) == {
        DEPLOYMENT_EVIDENCE_TABLE,
        DEPLOYMENT_READINESS_TABLE,
    }
    for table in (DEPLOYMENT_EVIDENCE_TABLE, DEPLOYMENT_READINESS_TABLE):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    projection_api = contract["projection_api"]  # type: ignore[index]
    declared_tables = {entry["table"] for entry in projection_api["exposures"]}
    assert declared_tables == {DEPLOYMENT_EVIDENCE_TABLE, DEPLOYMENT_READINESS_TABLE}
    for entry in projection_api["exposures"]:
        for column in entry["columns"]:
            assert column in sql, f"{entry['table']}.{column} missing from migration"


def test_validation_event_updates_reducer_projection_tables() -> None:
    """A PASSED evidence-validated event materializes both projection tables."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerDeploymentEvidenceReducer()

    result = handler.project(
        _passing_validation(deployment_id="deploy-omn-12578", run="run-1"),
        db,
    )

    assert result.readiness_state == "READY"
    assert result.rows_upserted == 2
    assert set(result.tables) == {
        DEPLOYMENT_EVIDENCE_TABLE,
        DEPLOYMENT_READINESS_TABLE,
    }

    evidence_rows = db.query(
        DEPLOYMENT_EVIDENCE_TABLE, {"deployment_id": "deploy-omn-12578"}
    )
    assert len(evidence_rows) == 1
    assert evidence_rows[0]["evidence_lifecycle_state"] == "VALIDATED"
    assert evidence_rows[0]["validation_run_id"] == "run-1"

    readiness_rows = db.query(
        DEPLOYMENT_READINESS_TABLE, {"deployment_id": "deploy-omn-12578"}
    )
    assert len(readiness_rows) == 1
    assert readiness_rows[0]["readiness_state"] == "READY"


def test_readiness_is_derived_from_reducer_owned_projection_state() -> None:
    """Readiness reflects reducer-owned projection rows, not logs/summaries.

    A later FAILED validation for the same deployment supersedes readiness in
    the projection; the readiness row is the authority the readiness gate reads.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerDeploymentEvidenceReducer()

    handler.project(
        _passing_validation(deployment_id="deploy-omn-12578", run="run-1"), db
    )
    ready_rows = db.query(
        DEPLOYMENT_READINESS_TABLE, {"deployment_id": "deploy-omn-12578"}
    )
    assert ready_rows[0]["readiness_state"] == "READY"

    blocked = handler.project(
        _failing_validation(deployment_id="deploy-omn-12578", run="run-2"), db
    )
    assert blocked.readiness_state == "BLOCKED"

    blocked_rows = db.query(
        DEPLOYMENT_READINESS_TABLE, {"deployment_id": "deploy-omn-12578"}
    )
    assert len(blocked_rows) == 1
    assert blocked_rows[0]["readiness_state"] == "BLOCKED"
    assert "missing_dod_items" in blocked_rows[0]["blocking_reason_codes"]


def test_reducer_handle_requires_database_adapter() -> None:
    """The runtime injects the projection DB via ``_db``; absence fails fast."""
    handler = HandlerDeploymentEvidenceReducer()
    payload = _passing_validation(
        deployment_id="deploy-omn-12578", run="run-1"
    ).model_dump(mode="json")

    import pytest

    with pytest.raises(TypeError):
        handler.handle(payload)


def test_reducer_handle_projects_through_injected_db() -> None:
    """``handle`` with an injected ``_db`` materializes the projection."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerDeploymentEvidenceReducer()
    payload = _passing_validation(
        deployment_id="deploy-omn-12578", run="run-1"
    ).model_dump(mode="json")
    payload["_db"] = db

    out = handler.handle(payload)

    assert out["readiness_state"] == "READY"
    assert db.query(DEPLOYMENT_EVIDENCE_TABLE, {"deployment_id": "deploy-omn-12578"})
    assert db.query(DEPLOYMENT_READINESS_TABLE, {"deployment_id": "deploy-omn-12578"})
