# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13708 — Adapter DI local defaults (Systemic Finding #2).

Each node below previously crashed or emitted vacuous "clean" data when run
over the canonical in-memory/local bus (no Kafka/remote adapter injected).
These tests exercise every handler with NO remote adapter — i.e. exactly the
default local-bus configuration — and assert the node now degrades honestly
(local default impl or a loud refusal), never crashing and never reporting a
vacuous false-green.

Counter-example followed: node_recall_compute degrades honestly rather than
fabricating data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    DashboardSweepRequest,
    NodeDashboardSweep,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.handlers.handler_dep_cascade_dedup_orchestrator import (
    HandlerDepCascadeDedupOrchestrator,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)

# ---------------------------------------------------------------------------
# 1. node_observability_sink_effect — in-memory sink is the default
# ---------------------------------------------------------------------------
from omnimarket.nodes.node_observability_sink_effect.handlers.handler_observability_sink_effect import (
    HandlerObservabilitySinkEffect,
)
from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_input import (
    ModelActionEvent,
    ModelObservabilitySinkInput,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.handlers.handler_pipeline_audit_orchestrator import (
    HandlerPipelineAuditOrchestrator,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request import (
    ModelPipelineAuditRequest,
)
from omnimarket.nodes.node_runtime_sweep.handlers.handler_runtime_sweep import (
    NodeRuntimeSweep,
    RuntimeSweepRequest,
)
from omnimarket.nodes.node_tech_debt_sweep_orchestrator.handlers.handler_tech_debt_sweep_orchestrator import (
    HandlerTechDebtSweepOrchestrator,
)
from omnimarket.nodes.node_tech_debt_sweep_orchestrator.models.model_tech_debt_sweep_request import (
    ModelTechDebtSweepRequest,
)
from omnimarket.nodes.node_ticketing_insights_compute.handlers.handler_ticketing_insights import (
    NodeTicketingInsightsCompute,
    TicketingInsightsRequest,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
    HandlerVerificationSweepOrchestrator,
    LocalReceiptWriter,
)
from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
    ModelVerificationSweepOrchestratorRequest,
)

_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_observability_sink_defaults_to_inmemory_over_local_bus() -> None:
    """Both sinks requested, no adapters injected → persists in memory, no raise."""
    handler = HandlerObservabilitySinkEffect(clock=lambda: _NOW)
    request = ModelObservabilitySinkInput(
        correlation_id=uuid4(),
        session_id=uuid4(),
        events=(
            ModelActionEvent(
                event_id=uuid4(),
                agent_name="agent-test",
                action_type="tool_call",
                action_name="Read",
                emitted_at=_NOW,
            ),
        ),
        sink_kafka=True,
        sink_postgres=True,
        submitted_at=_NOW,
    )

    result = await handler.handle(request)

    assert result.persisted_event_count == 1
    assert result.kafka_trace_ids[0].startswith("inmemory:")
    assert len(result.postgres_row_ids) == 1


@pytest.mark.unit
def test_pipeline_audit_defaults_to_local_ticket_store(tmp_path: Path) -> None:
    """No ticket adapter + live run with findings → local ticket store, no raise."""
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)  # _looks_like_repo, but no entrypoint
    handler = HandlerPipelineAuditOrchestrator()  # no ticket adapter injected
    request = ModelPipelineAuditRequest(
        repos=("myrepo",),
        omni_home_path=str(tmp_path),
        dry_run=False,
        skip_ticket_creation=False,
    )

    result = handler.handle(request)

    assert result.gap_register, "expected at least one finding (missing entrypoint)"
    assert result.tickets_created, "local ticket store should record findings"
    assert all(tid.startswith("local-ticket-") for tid in result.tickets_created)


@pytest.mark.unit
def test_tech_debt_sweep_defaults_to_local_linear_adapter(tmp_path: Path) -> None:
    """No linear adapter + live run with findings → local adapter, no raise."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "mod.py").write_text("# TODO: pay down this debt\n", encoding="utf-8")
    handler = HandlerTechDebtSweepOrchestrator()  # no linear adapter injected
    request = ModelTechDebtSweepRequest(
        repos=("myrepo",),
        categories=("todo-fixme",),
        omni_home=str(tmp_path),
        dry_run=False,
    )

    result = handler.handle(request)

    assert result.total_findings >= 1
    assert result.total_tickets_created >= 1


@pytest.mark.unit
def test_dep_cascade_dedup_defaults_to_local_github_adapter() -> None:
    """No github adapter → local adapter returns no data, no raise, honest zeros."""
    handler = HandlerDepCascadeDedupOrchestrator()  # no adapter injected
    result = handler.handle(ModelDepCascadeDedupRequest(dry_run=False))

    assert result.repos_scanned == 0
    assert result.groups_found == 0
    assert result.prs_closed == 0


@pytest.mark.unit
def test_verification_sweep_defaults_to_local_receipt_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No receipt writer + live run → local receipt writer, no adapter error."""
    monkeypatch.chdir(tmp_path)
    handler = HandlerVerificationSweepOrchestrator()  # no adapters injected
    result = handler.handle(
        ModelVerificationSweepOrchestratorRequest(dry_run=False),
    )

    assert result.adapter_errors == []
    assert result.overall_status != "fail"
    assert result.receipt_path
    assert Path(result.receipt_path).exists()


@pytest.mark.unit
def test_local_receipt_writer_writes_durable_local_evidence(tmp_path: Path) -> None:
    writer = LocalReceiptWriter(state_dir=tmp_path / "receipts")
    path = writer.write_receipt({"k": "v"})
    assert Path(path).exists()


@pytest.mark.unit
def test_ticketing_insights_fails_loud_instead_of_epoch_zero() -> None:
    """Empty input must fail loud, not emit all-zero velocity with 1969 dates."""
    handler = NodeTicketingInsightsCompute()
    with pytest.raises(ValueError, match="epoch-0"):
        handler.handle(TicketingInsightsRequest())


@pytest.mark.unit
def test_ticketing_insights_dates_not_in_epoch_when_data_supplied() -> None:
    """With dated data, trend dates anchor to the data — never 1969/1970."""
    handler = NodeTicketingInsightsCompute()
    result = handler.handle(
        TicketingInsightsRequest(
            ticket_data=[
                {
                    "identifier": "OMN-1",
                    "state": "Done",
                    "completedAt": "2026-06-20T00:00:00",
                }
            ],
        )
    )
    assert result.trend_data is not None
    for point in result.trend_data.daily_velocity:
        assert not point["date"].startswith("196")
        assert not point["date"].startswith("1970")


@pytest.mark.unit
def test_runtime_sweep_zero_entities_is_not_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sweep that checks nothing must FAIL loudly, never report clean.

    OMN-13919 hardened the OMN-13708 behavior: an empty request now resolves
    the default ``$OMNI_HOME`` contract set, and a run that still checks zero
    entities raises instead of returning a reportable ``no_input`` result
    (the dispatch layer mapped any returned result to success).
    """
    # Empty $OMNI_HOME ⇒ default collection finds nothing ⇒ hard failure.
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="zero entities"):
        NodeRuntimeSweep().handle(RuntimeSweepRequest())

    # No $OMNI_HOME at all ⇒ hard failure, never a silent empty default.
    monkeypatch.delenv("OMNI_HOME", raising=False)
    with pytest.raises(ValueError, match="OMNI_HOME"):
        NodeRuntimeSweep().handle(RuntimeSweepRequest())


@pytest.mark.unit
def test_dashboard_sweep_zero_pages_is_not_clean() -> None:
    """No base_url and no pages — examined nothing, must report no_targets."""
    result = NodeDashboardSweep().handle(DashboardSweepRequest())
    assert result.pages_total == 0
    assert result.status == "no_targets"
    assert result.status != "clean"
