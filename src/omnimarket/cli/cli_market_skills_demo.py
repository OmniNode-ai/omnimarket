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
    task_text: str
    pitch_claim: str
    skill_names: tuple[str, ...]
    market_nodes: tuple[str, ...]
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
    task_text: str
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
        task_text=(
            "Route one coding task to the local Qwen profile, compare it to "
            "the GLM cloud baseline, and show the projected savings row."
        ),
        pitch_claim=(
            "Routes model work to the cheapest capable local profile and "
            "materializes the savings row."
        ),
        skill_names=(),
        market_nodes=(
            "node_model_router",
            "node_projection_delegation",
            "node_projection_llm_cost",
            "node_projection_savings",
        ),
        command_hint="uv run delegation-cost-demo --output table",
        value_tags=("delegation", "cost-savings", "projection"),
    ),
    DemoDefinition(
        demo_id="all-model-cost-arbitrage",
        title="All-Model Cost Arbitrage",
        task_text=(
            "Run the same two coding tasks across every configured local model "
            "plus GLM so the model market is visible in one table."
        ),
        pitch_claim=(
            "Runs every configured local model plus the GLM cloud baseline so "
            "the audience can see speed, token, and marginal API cost deltas."
        ),
        skill_names=(),
        market_nodes=(
            "node_ab_compare_orchestrator",
            "node_ab_inference_effect",
            "node_ab_compare_reducer",
            "node_model_router",
        ),
        command_hint="uv run ab-compare-suite --models all --output table",
        value_tags=("cost-savings", "model-routing", "cloud-baseline"),
    ),
    DemoDefinition(
        demo_id="merge-delegation",
        title="PR Lifecycle Delegation",
        task_text=(
            "Inventory a PR and delegate lifecycle planning to the contracted "
            "PR orchestrator."
        ),
        pitch_claim=(
            "Delegates PR inventory, fix, verify, and merge decisions into a "
            "contracted market node."
        ),
        skill_names=("pr_lifecycle_orchestrator",),
        market_nodes=(),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills pr_lifecycle_orchestrator --skip-pytest --stream"
        ),
        value_tags=("delegation", "merge-readiness", "human-time-savings"),
    ),
    DemoDefinition(
        demo_id="review-cost-control",
        title="Review Work Deflection",
        task_text=(
            "Run local review, CodeRabbit thread triage, and PR polish before "
            "spending higher-cost review attention."
        ),
        pitch_claim=(
            "Uses local review, CodeRabbit triage, and PR polish before "
            "spending human attention on review cleanup."
        ),
        skill_names=("local_review", "coderabbit_triage", "pr_polish"),
        market_nodes=(),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills local_review,coderabbit_triage,pr_polish --skip-pytest --stream"
        ),
        value_tags=("delegation", "review-automation", "cost-avoidance"),
    ),
    DemoDefinition(
        demo_id="session-dispatch",
        title="Session to Ticket Dispatch",
        task_text=(
            "Bootstrap a session, score ticket work, and produce dispatch "
            "artifacts for downstream operators."
        ),
        pitch_claim=(
            "Bootstraps a session, composes dispatch artifacts, and proves a "
            "ticket pipeline can compile delegated work."
        ),
        skill_names=("session_bootstrap", "session_orchestrator", "ticket_pipeline"),
        market_nodes=(),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills session_bootstrap,session_orchestrator,ticket_pipeline "
            "--skip-pytest --stream"
        ),
        value_tags=("orchestration", "delegation", "workflow-proof"),
    ),
    DemoDefinition(
        demo_id="review-escalation-gate",
        title="Review Escalation Gate",
        task_text=(
            "Attempt cheap/local review and cleanup first, then identify what "
            "would justify escalation to a more expensive model or operator."
        ),
        pitch_claim=(
            "Uses cheap local review and deterministic cleanup first, then "
            "escalates only unresolved review work to higher-cost assistance."
        ),
        skill_names=("local_review", "coderabbit_triage", "aislop_sweep", "pr_polish"),
        market_nodes=("node_model_router", "node_projection_llm_cost"),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills local_review,coderabbit_triage,aislop_sweep,pr_polish "
            "--skip-pytest --stream"
        ),
        value_tags=("review-automation", "cost-avoidance", "escalation-control"),
    ),
    DemoDefinition(
        demo_id="ticket-intake-routing",
        title="Ticket Intake to Market Dispatch",
        task_text=(
            "Convert a session/ticket brief into dispatchable market work and "
            "receipt artifacts."
        ),
        pitch_claim=(
            "Turns a session/ticket brief into dispatchable market work, "
            "showing the platform can route work before humans pick operators."
        ),
        skill_names=("session_bootstrap", "ticket_pipeline", "session_orchestrator"),
        market_nodes=("node_build_dispatch_effect", "node_dispatch_worker"),
        command_hint=(
            "uv run python scripts/run_market_skill_baseline.py "
            "--skills session_bootstrap,ticket_pipeline,session_orchestrator "
            "--skip-pytest --stream"
        ),
        value_tags=("delegation", "market-dispatch", "workflow-proof"),
    ),
    DemoDefinition(
        demo_id="pr-health-triage",
        title="PR Health Triage Market",
        task_text=(
            "Classify PRs into stale, red, conflicted, review-blocked, and "
            "healthy queues for machine-actionable triage."
        ),
        pitch_claim=(
            "Classifies stale, red, conflicted, and review-blocked PRs into "
            "machine-actionable queues without spending senior review time."
        ),
        skill_names=(),
        market_nodes=(
            "node_pr_snapshot_effect",
            "node_pr_health_monitor",
            "node_linear_triage",
            "node_create_ticket",
        ),
        command_hint="uv run pytest tests/test_golden_chain_pr_health_monitor.py -q",
        value_tags=("triage", "delegation", "human-time-savings"),
    ),
    DemoDefinition(
        demo_id="review-thread-policy-control",
        title="Review Thread Policy Control",
        task_text=(
            "Prove review-thread replies stay draft-first and policy bypasses "
            "can be reconciled."
        ),
        pitch_claim=(
            "Keeps review-thread actions governed: draft replies by default "
            "and re-open threads when non-bot actors bypass policy."
        ),
        skill_names=(),
        market_nodes=(
            "node_thread_reply_effect",
            "node_review_thread_reconciler",
            "node_finding_aggregator_compute",
        ),
        command_hint=(
            "uv run pytest tests/nodes/node_thread_reply_effect/"
            "test_handler_thread_reply.py "
            "tests/test_golden_chain_review_thread_reconciler.py -q"
        ),
        value_tags=("governance", "review-automation", "cost-avoidance"),
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
        task_text=definition.task_text,
        pitch_claim=definition.pitch_claim,
        value_tags=list(definition.value_tags),
        command_hint=definition.command_hint,
        market_nodes=[
            *definition.market_nodes,
            *(proof.node_name for proof in skill_proofs),
        ],
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
    demo_width = max(24, *(len(demo.demo_id) for demo in report.demos))
    tag_width = max(42, *(_tag_width(demo) for demo in report.demos))
    header = (
        f"{'demo':<{demo_width}} {'status':<16} {'skills':>6} "
        f"{'value_tags':<{tag_width}} {'command'}"
    )
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
            f"{demo.demo_id:<{demo_width}} {demo.proof_status:<16} "
            f"{skill_count:>6} {tags:<{tag_width}} {demo.command_hint}"
        )
        click.echo(f"  task: {demo.task_text}")
        click.echo(f"  claim: {demo.pitch_claim}")
        if demo.market_nodes:
            click.echo(f"  nodes: {', '.join(demo.market_nodes)}")


def _tag_width(demo: ModelMarketSkillsDemo) -> int:
    return len(",".join(demo.value_tags))


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
