# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_pipeline_audit_orchestrator.

WS-5 Wave 8 (OMN-13682). The node's handler is a synchronous, deterministic
``handle(request) -> result`` over a repo inventory (it is *not* an async
bus-consumer — there is no start-command/terminal-event surface to round-trip
through the bus). The faithful integration test drives the handler in-process
against a *synthetic omni_home tree* built under ``tmp_path`` and injects the
Linear ticket boundary via a mock adapter (the ``_Mock*`` collaborator pattern).
NO subprocess/asyncpg is monkeypatched; the I/O boundary is the injected adapter.

Each case varies ``audit_type`` and the execution flags (dry_run, fail_fast,
skip_ticket_creation) and asserts the typed ``ModelPipelineAuditResult``
(run_status, severity counts, gap-register proof categories, tickets created).

Negative control: a repo subscribing to a topic with no audited producer must
yield a BREAKING finding. A clean run over this tree would be a regression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pipeline_audit_orchestrator.handlers.handler_pipeline_audit_orchestrator import (
    HandlerPipelineAuditOrchestrator,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request import (
    EnumAuditType,
    ModelPipelineAuditRequest,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_result import (
    EnumFindingSeverity,
    EnumPipelineAuditStatus,
    EnumProofCategory,
)


class _MockTicketAdapter:
    """Injected boundary capturing remediation-ticket payloads."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_ticket(self, payload: dict[str, Any]) -> str:
        self.created.append(payload)
        return f"OMN-{len(self.created)}"


def _write_contract(
    omni_home: Path,
    repo: str,
    *,
    publish: tuple[str, ...] = (),
    subscribe: tuple[str, ...] = (),
) -> None:
    pkg = omni_home / repo / "src" / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (omni_home / repo / "pyproject.toml").write_text(
        f'[project]\nname = "{repo}"\n', encoding="utf-8"
    )
    publish_block = "\n".join(f"    - {topic}" for topic in publish) or "    []"
    subscribe_block = "\n".join(f"    - {topic}" for topic in subscribe) or "    []"
    (pkg / "contract.yaml").write_text(
        f"""---
name: {repo}
event_bus:
  publish_topics:
{publish_block}
  subscribe_topics:
{subscribe_block}
""",
        encoding="utf-8",
    )


def _build_omni_home(tmp_path: Path) -> Path:
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir(parents=True, exist_ok=True)
    # alpha: orphan producer (publishes a topic nobody consumes) → HIGH.
    _write_contract(omni_home, "alpha", publish=("onex.evt.alpha.v1",))
    # beta: ghost consumer (subscribes to a topic nobody produces) → BREAKING.
    _write_contract(omni_home, "beta", subscribe=("onex.evt.ghost.v1",))
    # gamma: a repo with source but no runtime entrypoint → entrypoint MISSING.
    gamma_src = omni_home / "gamma" / "src"
    gamma_src.mkdir(parents=True, exist_ok=True)
    (gamma_src / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    return omni_home


_REPOS = ("alpha", "beta", "gamma")


@pytest.mark.integration
def test_pipeline_audit_full_creates_tickets_for_every_finding(tmp_path: Path) -> None:
    omni_home = _build_omni_home(tmp_path)
    adapter = _MockTicketAdapter()

    result = HandlerPipelineAuditOrchestrator(ticket_adapter=adapter).handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=EnumAuditType.FULL,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is EnumPipelineAuditStatus.COMPLETED
    assert set(result.repos_audited) == set(_REPOS)
    assert result.gap_register, "full audit produced no findings"
    # Negative control: ghost subscription is a BREAKING finding.
    assert result.breaking_count >= 1
    assert result.high_count >= 1
    breaking = [
        f for f in result.gap_register if f.severity is EnumFindingSeverity.BREAKING
    ]
    assert any("ghost" in f.description for f in breaking), breaking
    # One ticket per finding, severity-ordered finding ids are dense + 1-based.
    assert len(adapter.created) == len(result.gap_register)
    assert len(result.tickets_created) == len(result.gap_register)
    assert [f.finding_id for f in result.gap_register] == list(
        range(1, len(result.gap_register) + 1)
    )


@pytest.mark.integration
def test_pipeline_audit_topics_only_scope(tmp_path: Path) -> None:
    omni_home = _build_omni_home(tmp_path)

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=EnumAuditType.TOPICS,
            skip_ticket_creation=True,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is EnumPipelineAuditStatus.COMPLETED
    assert result.gap_register
    assert all(
        f.proof_category is EnumProofCategory.WIRE_TOPICS for f in result.gap_register
    )
    assert result.breaking_count >= 1
    assert result.high_count >= 1
    assert result.tickets_created == ()  # skip_ticket_creation


@pytest.mark.integration
def test_pipeline_audit_entrypoint_only_scope(tmp_path: Path) -> None:
    omni_home = _build_omni_home(tmp_path)

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=EnumAuditType.ENTRYPOINT,
            skip_ticket_creation=True,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is EnumPipelineAuditStatus.COMPLETED
    assert result.gap_register
    assert all(
        f.proof_category is EnumProofCategory.ENTRYPOINT for f in result.gap_register
    )
    # gamma is the only repo missing a runtime entrypoint.
    assert any("gamma" in f.description for f in result.gap_register)


@pytest.mark.integration
def test_pipeline_audit_dry_run_creates_no_tickets(tmp_path: Path) -> None:
    omni_home = _build_omni_home(tmp_path)
    adapter = _MockTicketAdapter()

    result = HandlerPipelineAuditOrchestrator(ticket_adapter=adapter).handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=EnumAuditType.FULL,
            dry_run=True,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is EnumPipelineAuditStatus.DRY_RUN
    assert result.dry_run is True
    assert result.gap_register  # findings still computed
    assert adapter.created == []  # no ticket I/O in dry-run
    assert result.tickets_created == ()


@pytest.mark.integration
def test_pipeline_audit_fail_fast_aborts_on_breaking(tmp_path: Path) -> None:
    omni_home = _build_omni_home(tmp_path)

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=EnumAuditType.FULL,
            fail_fast=True,
            skip_ticket_creation=True,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is EnumPipelineAuditStatus.ABORTED
    assert result.breaking_count >= 1
