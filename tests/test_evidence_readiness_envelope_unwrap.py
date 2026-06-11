# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for OMN-12936 and OMN-12946.

OMN-12946 adds coverage for the dispatch-engine *materialized* envelope shape
(``{payload, __bindings, __debug_trace}``) that the live runtime actually
delivers — the earlier fixtures only exercised top-level transport markers and
so passed while the live path failed.

Two defects classified BROKEN in the 2026-06-11 full-feature pass:

1. The evidence/readiness ``coerce(**payload)`` family splatted the runtime
   transport envelope (``{"payload": {...}, "partition_key": ...}``) straight
   into domain-model construction, raising a ``ValidationError`` with every
   required field reported missing. The readiness gate orchestrator shared this
   defect class with the evidence pipeline.
2. ``projection_overnight_readiness`` (a view over ``overnight_sessions``) had
   no node-owned migration to create its base tables, so the projection-api
   served HTTP 503 ``table 'public.projection_overnight_readiness' not found at
   startup``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)

from omnimarket.nodes.evidence_pipeline_native import (
    coerce_command,
    coerce_gap,
    coerce_readiness,
)
from omnimarket.nodes.node_readiness_gate_orchestrator import (
    HandlerReadinessGateOrchestrator,
)

NODE_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_overnight/migrations"
)


def _gap_report() -> ModelGapReport:
    return ModelGapReport(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        deployment_id="deploy-omn-12936",
        generated_at="2026-06-11T02:00:00Z",
        validator_version="evidence-readiness-native-v1",
        gap_classifications={},
        validation_result_refs=("sha256:ref-1",),
    )


def _readiness_result() -> ModelDeploymentReadinessResult:
    return ModelDeploymentReadinessResult(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        deployment_id="deploy-omn-12936",
        readiness_state="READY",
        scored_at="2026-06-11T02:00:00Z",
        validator_version="evidence-readiness-native-v1",
        gap_report_hash="sha256:gap-hash",
    )


def _command() -> ModelEvidencePipelineCommand:
    return ModelEvidencePipelineCommand(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        ticket_id="OMN-12936",
        repository="omnimarket",
        source_commit_sha="abcdef1234567890",
        requested_at="2026-06-11T02:00:00Z",
        trigger_surface="manual",
    )


def _enveloped(model: object) -> dict[str, object]:
    """Mimic the dispatch-engine materialized envelope around a domain payload."""
    return {
        "payload": model.model_dump(mode="json"),  # type: ignore[attr-defined]
        "partition_key": None,
        "event_type": "onex.cmd.omnimarket.readiness-gate-start.v1",
    }


def test_coerce_gap_unwraps_transport_envelope() -> None:
    gap = _gap_report()
    coerced = coerce_gap(_enveloped(gap))
    assert coerced == gap


def test_coerce_readiness_unwraps_transport_envelope() -> None:
    readiness = _readiness_result()
    coerced = coerce_readiness(_enveloped(readiness))
    assert coerced == readiness


def test_coerce_command_unwraps_transport_envelope() -> None:
    command = _command()
    coerced = coerce_command(_enveloped(command))
    assert coerced == command


def test_coerce_gap_still_accepts_bare_payload() -> None:
    gap = _gap_report()
    assert coerce_gap(gap.model_dump(mode="json")) == gap
    assert coerce_gap(gap) is gap


# ---------------------------------------------------------------------------
# OMN-12946: dispatch-engine *materialized* envelope shape.
#
# The previous fixtures (``_enveloped``) carry top-level transport markers
# (``partition_key``/``event_type``) and therefore matched the marker predicate
# even before this fix. That is NOT the shape the live runtime delivers.
#
# ``MessageDispatchEngine._execute_dispatcher`` ALWAYS pre-materializes the
# envelope via ``_materialize_envelope_with_bindings``, whose outer keys are
# exactly ``{"payload", "__bindings", "__debug_trace"}`` (the real
# ``partition_key`` lives *inside* ``__debug_trace``, not at the top level).
# Because the evidence/readiness contracts declare ``handler_routing`` with no
# ``event_model``, auto-wiring passes this materialized dict RAW to ``handle()``,
# so the omnimarket-side ``_unwrap_envelope`` is the only thing that can strip
# the wrapper. The handler-isolation fixtures above passed while the live path
# failed — these tests pin the real dispatch shape so that gap cannot recur.
# ---------------------------------------------------------------------------


def _materialized(model: object) -> dict[str, object]:
    """Mimic ``MessageDispatchEngine._materialize_envelope_with_bindings``.

    The outer keys are exactly the three the dispatch engine emits; nothing
    else is carried at the top level. Mirrors
    ``omnibase_infra.runtime.message_dispatch_engine`` (build commit b7c93a8e):
    ``{"payload": <domain-json>, "__bindings": {}, "__debug_trace": {...}}``.
    """
    return {
        "payload": model.model_dump(mode="json"),  # type: ignore[attr-defined]
        "__bindings": {},
        "__debug_trace": {
            "event_type": "onex.cmd.omnimarket.evidence-pipeline-start.v1",
            "partition_key": None,
        },
    }


def test_coerce_command_unwraps_materialized_dispatch_envelope() -> None:
    command = _command()
    coerced = coerce_command(_materialized(command))
    assert coerced == command


def test_coerce_gap_unwraps_materialized_dispatch_envelope() -> None:
    gap = _gap_report()
    coerced = coerce_gap(_materialized(gap))
    assert coerced == gap


def test_coerce_readiness_unwraps_materialized_dispatch_envelope() -> None:
    readiness = _readiness_result()
    coerced = coerce_readiness(_materialized(readiness))
    assert coerced == readiness


def test_evidence_orchestrator_handles_materialized_envelope() -> None:
    """The orchestrator handler reached via the real dispatch shape.

    Reproduces ``second-site/repro_real_dispatch.py``: the handler is fed the
    dispatch-engine-materialized dict (not a ``ModelEventEnvelope`` object and
    not a ``{payload, partition_key}`` dict). With the marker-set drift this
    raised ``ValidationError`` (10 errors) before reaching any port.
    """
    from omnimarket.nodes.node_evidence_pipeline_orchestrator.handlers.handler_evidence_pipeline_orchestrator import (
        HandlerEvidencePipelineOrchestrator,
    )

    class _StubPorts:
        def collect(self, command: object) -> object:
            return command

        def extract(self, raw: object) -> object:
            return raw

        def match_contract(self, bundle: object) -> object:
            return bundle

        def write_occ_pr(self, validation: object) -> object:
            return validation

        def update_linear(self, validation: object) -> None:
            return None

        def publish(self, value: object) -> None:
            return None

    command = _command()
    handler = HandlerEvidencePipelineOrchestrator(ports=_StubPorts())  # type: ignore[arg-type]
    result = handler.handle(_materialized(command))
    assert isinstance(result, ModelEvidencePipelineCommand)
    assert result.correlation_id == command.correlation_id


def test_marker_set_covers_materialized_dispatch_keys() -> None:
    """Consistency guard: the marker set must intersect the materialized shape.

    ``_unwrap_envelope`` only unwraps when ``_ENVELOPE_MARKER_KEYS`` intersects
    the outer mapping's keys. The dispatch engine's materialized outer keys are
    ``{"payload", "__bindings", "__debug_trace"}``. If a future edit drops the
    materialization markers, ``_unwrap_envelope`` silently becomes a no-op again
    and every domain field is reported missing (the OMN-12946 / OMN-12935 /
    OMN-12940 live failure). This pins the invariant in-repo and mirrors the
    infra-side ``handler_wiring._ENVELOPE_MARKER_KEYS`` (OMN-12940).
    """
    from omnimarket.nodes.evidence_pipeline_native import _ENVELOPE_MARKER_KEYS

    # The exact outer keys produced by
    # MessageDispatchEngine._materialize_envelope_with_bindings.
    materialized_outer_keys = {"payload", "__bindings", "__debug_trace"}
    intersection = _ENVELOPE_MARKER_KEYS & materialized_outer_keys
    assert intersection, (
        "_ENVELOPE_MARKER_KEYS must intersect the dispatch-engine materialized "
        "outer keys {payload, __bindings, __debug_trace}; otherwise "
        "_unwrap_envelope is a no-op on live dispatch and domain coercion fails."
    )


def test_readiness_gate_handles_enveloped_gap_report() -> None:
    gap = _gap_report()
    result = HandlerReadinessGateOrchestrator().handle(_enveloped(gap))
    assert isinstance(result, ModelDeploymentReadinessResult)
    assert result.readiness_state == "READY"
    assert result.correlation_id == gap.correlation_id


def test_readiness_gate_routes_enveloped_readiness_result_directly() -> None:
    readiness = _readiness_result()
    result = HandlerReadinessGateOrchestrator().handle(_enveloped(readiness))
    assert result.readiness_state == "READY"
    assert result.gap_report_hash == readiness.gap_report_hash


def test_readiness_gate_still_handles_bare_gap_report() -> None:
    gap = _gap_report()
    result = HandlerReadinessGateOrchestrator().handle(gap)
    assert result.correlation_id == gap.correlation_id


def test_overnight_sessions_base_migration_exists_before_view() -> None:
    base = NODE_MIGRATIONS / "0000_create_overnight_sessions_tables.sql"
    view = NODE_MIGRATIONS / "0001_create_overnight_readiness_projection_view.sql"
    assert base.exists(), "base-table migration must exist for the view"
    # Filename order is the migration-runner apply order: base before view.
    assert base.name < view.name
    base_sql = base.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS overnight_sessions" in base_sql
    assert "CREATE TABLE IF NOT EXISTS overnight_session_phases" in base_sql


def test_view_migration_depends_on_base_table() -> None:
    view = NODE_MIGRATIONS / "0001_create_overnight_readiness_projection_view.sql"
    view_sql = view.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW projection_overnight_readiness" in view_sql
    assert "FROM overnight_sessions" in view_sql


@pytest.mark.parametrize("migration_name", ["0000", "0001"])
def test_node_migrations_are_idempotent(migration_name: str) -> None:
    matches = list(NODE_MIGRATIONS.glob(f"{migration_name}_*.sql"))
    assert len(matches) == 1
    sql = matches[0].read_text(encoding="utf-8")
    # Re-application over an infra-seeded DB must be a no-op.
    assert "IF NOT EXISTS" in sql or "CREATE OR REPLACE" in sql
