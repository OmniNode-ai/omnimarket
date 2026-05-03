# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Market skill demo catalog CLI.

This command turns the existing market-skill baseline into a demo-facing proof
surface. It keeps the baseline as the source of truth for node contracts and
smoke commands, while grouping valuable skills into pitchable workflows.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.market_skill_baseline import (
    ModelMarketSkillResult,
    ModelMarketSkillSpec,
    capture_market_skill_result,
    iter_market_skill_specs,
)


@dataclass(frozen=True)
class DemoDefinition:
    """Curated demo lane backed by market nodes or an existing proof CLI."""

    demo_id: str
    title: str
    pitch_claim: str
    skill_names: tuple[str, ...]
    command_hint: str
    value_tags: tuple[str, ...]


class ModelSkillDemoProof(BaseModel):
    """One skill proof result inside a demo lane."""

    model_config = ConfigDict(extra="forbid")

    skill_name: str
    node_name: str
    terminal_event: str | None = None
    status: str
    cli_smoke: str
    command: list[str] = Field(default_factory=list)
    summary: dict[str, object] = Field(default_factory=dict)


class ModelMarketSkillsDemo(BaseModel):
    """Rendered demo lane with optional smoke proof."""

    model_config = ConfigDict(extra="forbid")

    demo_id: str
    title: str
    pitch_claim: str
    value_tags: list[str]
    command_hint: str
    market_nodes: list[str]
    proof_status: str
    working_skills: int
    total_skills: int
    skill_proofs: list[ModelSkillDemoProof] = Field(default_factory=list)


class ModelMarketSkillsDemoReport(BaseModel):
    """Catalog report for selected demo lanes."""

    model_config = ConfigDict(extra="forbid")

    run_smokes: bool
    include_pytest: bool
    demos: list[ModelMarketSkillsDemo]


_DEMO_DEFINITIONS: tuple[DemoDefinition, ...] = (
    DemoDefinition(
        demo_id="cost-routing-projection",
        title="Delegation + Cost Savings Projection",
        pitch_claim=(
            "Routes model work to the cheapest capable local profile and "
            "materializes the savings row."
        ),
        skill_names=(),
        command_hint="uv run delegation-cost-demo --output table",
        value_tags=("delegation", "cost-savings", "projection"),
    ),
    DemoDefinition(
        demo_id="merge-delegation",
        title="PR Lifecycle Delegation",
        pitch_claim=(
            "Delegates PR inventory, fix, verify, and merge decisions into a "
            "contracted market node."
        ),
        skill_names=("pr_lifecycle_orchestrator",),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills pr_lifecycle_orchestrator --skip-pytest --stream"
        ),
        value_tags=("delegation", "merge-readiness", "human-time-savings"),
    ),
    DemoDefinition(
        demo_id="review-cost-control",
        title="Review Work Deflection",
        pitch_claim=(
            "Uses local review, CodeRabbit triage, and PR polish before "
            "spending human attention on review cleanup."
        ),
        skill_names=("local_review", "coderabbit_triage", "pr_polish"),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills local_review,coderabbit_triage,pr_polish --skip-pytest --stream"
        ),
        value_tags=("delegation", "review-automation", "cost-avoidance"),
    ),
    DemoDefinition(
        demo_id="session-dispatch",
        title="Session to Ticket Dispatch",
        pitch_claim=(
            "Bootstraps a session, composes dispatch artifacts, and proves a "
            "ticket pipeline can compile delegated work."
        ),
        skill_names=("session_bootstrap", "session_orchestrator", "ticket_pipeline"),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills session_bootstrap,session_orchestrator,ticket_pipeline "
            "--skip-pytest --stream"
        ),
        value_tags=("orchestration", "delegation", "workflow-proof"),
    ),
)


def _demo_by_id() -> dict[str, DemoDefinition]:
    return {demo.demo_id: demo for demo in _DEMO_DEFINITIONS}


def _spec_by_skill_name() -> dict[str, ModelMarketSkillSpec]:
    return {spec.skill_name: spec for spec in iter_market_skill_specs()}


def _select_demos(demo_id: str) -> list[DemoDefinition]:
    if demo_id == "all":
        return list(_DEMO_DEFINITIONS)
    demos = _demo_by_id()
    selected = demos.get(demo_id)
    if selected is None:
        available = ", ".join(["all", *sorted(demos)])
        raise click.ClickException(f"Unknown demo {demo_id!r}. Available: {available}")
    return [selected]


def _skill_result_to_proof(result: ModelMarketSkillResult) -> ModelSkillDemoProof:
    return ModelSkillDemoProof(
        skill_name=result.skill_name,
        node_name=result.contract.node_name,
        terminal_event=result.contract.terminal_event,
        status=result.overall_status,
        cli_smoke="pass" if result.cli_smoke.passed else "fail",
        command=list(result.cli_smoke.command),
        summary=result.cli_smoke.summary,
    )


def _not_run_proof(skill_name: str) -> ModelSkillDemoProof:
    specs = _spec_by_skill_name()
    spec = specs[skill_name]
    return ModelSkillDemoProof(
        skill_name=skill_name,
        node_name=spec.node_name,
        terminal_event=None,
        status="not-run",
        cli_smoke="not-run",
        command=[],
        summary={},
    )


def _build_demo(
    definition: DemoDefinition,
    *,
    run_smokes: bool,
    include_pytest: bool,
) -> ModelMarketSkillsDemo:
    skill_proofs: list[ModelSkillDemoProof] = []
    for skill_name in definition.skill_names:
        if run_smokes:
            result = capture_market_skill_result(
                _spec_by_skill_name()[skill_name],
                run_pytest=include_pytest,
            )
            skill_proofs.append(_skill_result_to_proof(result))
        else:
            skill_proofs.append(_not_run_proof(skill_name))

    working = sum(1 for proof in skill_proofs if proof.status == "working")
    total = len(skill_proofs)
    if total == 0:
        proof_status = "external-command"
    elif working == total:
        proof_status = "working"
    elif any(proof.status == "not-run" for proof in skill_proofs):
        proof_status = "not-run"
    elif any(proof.status == "working" for proof in skill_proofs):
        proof_status = "degraded"
    else:
        proof_status = "failing"

    return ModelMarketSkillsDemo(
        demo_id=definition.demo_id,
        title=definition.title,
        pitch_claim=definition.pitch_claim,
        value_tags=list(definition.value_tags),
        command_hint=definition.command_hint,
        market_nodes=[proof.node_name for proof in skill_proofs],
        proof_status=proof_status,
        working_skills=working,
        total_skills=total,
        skill_proofs=skill_proofs,
    )


def _build_report(
    *,
    demo_id: str,
    run_smokes: bool,
    include_pytest: bool,
) -> ModelMarketSkillsDemoReport:
    return ModelMarketSkillsDemoReport(
        run_smokes=run_smokes,
        include_pytest=include_pytest,
        demos=[
            _build_demo(
                definition,
                run_smokes=run_smokes,
                include_pytest=include_pytest,
            )
            for definition in _select_demos(demo_id)
        ],
    )


def _render_table(report: ModelMarketSkillsDemoReport) -> None:
    click.echo("MARKET SKILL DEMO CATALOG")
    click.echo(
        f"run_smokes={str(report.run_smokes).lower()} "
        f"include_pytest={str(report.include_pytest).lower()}"
    )
    header = f"{'demo':<24} {'status':<16} {'skills':>6} {'value_tags':<42} {'command'}"
    click.echo(header)
    click.echo("-" * len(header))
    for demo in report.demos:
        tags = ",".join(demo.value_tags)
        skill_count = (
            "n/a"
            if demo.total_skills == 0
            else f"{demo.working_skills}/{demo.total_skills}"
        )
        click.echo(
            f"{demo.demo_id:<24} {demo.proof_status:<16} {skill_count:>6} "
            f"{tags:<42} {demo.command_hint}"
        )
        click.echo(f"  claim: {demo.pitch_claim}")
        if demo.market_nodes:
            click.echo(f"  nodes: {', '.join(demo.market_nodes)}")


def _render_json(report: ModelMarketSkillsDemoReport) -> None:
    click.echo(json.dumps(report.model_dump(mode="json"), indent=2))


@click.command()
@click.option(
    "--demo",
    "demo_id",
    default="all",
    show_default=True,
    help="Demo id to render, or 'all'.",
)
@click.option(
    "--run-smokes/--no-run-smokes",
    default=True,
    show_default=True,
    help="Run curated market-skill smoke commands.",
)
@click.option(
    "--include-pytest",
    is_flag=True,
    help="Also run each skill's focused pytest targets.",
)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)
def main(
    demo_id: str,
    run_smokes: bool,
    include_pytest: bool,
    output: Literal["table", "json"],
) -> None:
    """Render and optionally prove demo-ready market skill workflows."""

    report = _build_report(
        demo_id=demo_id,
        run_smokes=run_smokes,
        include_pytest=include_pytest,
    )
    if output == "json":
        _render_json(report)
    else:
        _render_table(report)
    sys.exit(0)


__all__ = ["main"]
