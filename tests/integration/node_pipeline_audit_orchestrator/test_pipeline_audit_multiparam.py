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
@pytest.mark.parametrize(
    (
        "audit_type",
        "dry_run",
        "fail_fast",
        "skip_ticket_creation",
        "expected_status",
        "expected_proof_category",
        "expect_breaking",
        "description_substr",
    ),
    [
        # FULL scope, default flags → tickets created for every finding.
        (
            EnumAuditType.FULL,
            False,
            False,
            False,
            EnumPipelineAuditStatus.COMPLETED,
            None,  # mixed proof categories
            True,
            "ghost",
        ),
        # TOPICS scope → only wire-topic findings; ticket creation skipped.
        (
            EnumAuditType.TOPICS,
            False,
            False,
            True,
            EnumPipelineAuditStatus.COMPLETED,
            EnumProofCategory.WIRE_TOPICS,
            True,
            "ghost",
        ),
        # ENTRYPOINT scope → only entrypoint findings; gamma is the missing one.
        (
            EnumAuditType.ENTRYPOINT,
            False,
            False,
            True,
            EnumPipelineAuditStatus.COMPLETED,
            EnumProofCategory.ENTRYPOINT,
            False,
            "gamma",
        ),
        # dry_run → findings still computed, no ticket I/O, DRY_RUN status.
        (
            EnumAuditType.FULL,
            True,
            False,
            False,
            EnumPipelineAuditStatus.DRY_RUN,
            None,
            True,
            "ghost",
        ),
        # fail_fast → BREAKING finding aborts the run.
        (
            EnumAuditType.FULL,
            False,
            True,
            True,
            EnumPipelineAuditStatus.ABORTED,
            None,
            True,
            "ghost",
        ),
    ],
    ids=["full", "topics-scope", "entrypoint-scope", "dry-run", "fail-fast"],
)
def test_pipeline_audit_multiparam_matrix(
    tmp_path: Path,
    audit_type: EnumAuditType,
    dry_run: bool,
    fail_fast: bool,
    skip_ticket_creation: bool,
    expected_status: EnumPipelineAuditStatus,
    expected_proof_category: EnumProofCategory | None,
    expect_breaking: bool,
    description_substr: str,
) -> None:
    omni_home = _build_omni_home(tmp_path)
    adapter = _MockTicketAdapter()

    result = HandlerPipelineAuditOrchestrator(ticket_adapter=adapter).handle(
        ModelPipelineAuditRequest(
            repos=_REPOS,
            audit_type=audit_type,
            dry_run=dry_run,
            fail_fast=fail_fast,
            skip_ticket_creation=skip_ticket_creation,
            omni_home_path=str(omni_home),
        )
    )

    assert result.run_status is expected_status
    assert result.dry_run is dry_run
    assert set(result.repos_audited) == set(_REPOS)
    assert result.gap_register, "audit produced no findings"
    assert any(description_substr in f.description for f in result.gap_register)

    if expected_proof_category is not None:
        assert all(
            f.proof_category is expected_proof_category for f in result.gap_register
        )
    if expect_breaking:
        # Negative control: a ghost subscription must surface a BREAKING finding.
        assert result.breaking_count >= 1

    # Severity-ordered finding ids are dense + 1-based regardless of scope.
    assert [f.finding_id for f in result.gap_register] == list(
        range(1, len(result.gap_register) + 1)
    )

    if dry_run or skip_ticket_creation:
        # No ticket I/O when dry-run or explicitly skipped.
        assert adapter.created == []
        assert result.tickets_created == ()
    else:
        # One ticket per finding, and the serialized payload carries the same
        # finding metadata — not just matching cardinality.
        assert len(adapter.created) == len(result.gap_register)
        assert len(result.tickets_created) == len(result.gap_register)
        assert [payload["finding_id"] for payload in adapter.created] == [
            finding.finding_id for finding in result.gap_register
        ]
        assert [payload["labels"][-1] for payload in adapter.created] == [
            finding.proof_category.value for finding in result.gap_register
        ]
