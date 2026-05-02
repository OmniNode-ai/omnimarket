# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AB compare CLI — thin rendering surface for HandlerAbCompareOrchestrator.

All business logic lives in the orchestrator. This file only:
  - Parses CLI args
  - Constructs ModelAbCompareStart and calls the handler
  - Renders results as a rich table or JSON
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
        ModelAbCompareResult,
    )


def _calculate_savings(result: ModelAbCompareResult) -> float:
    valid_costs = [row.cost_usd for row in result.comparison if not row.error]
    if not valid_costs:
        return 0.0
    return max(valid_costs) - min(valid_costs)


def _render_table(result: ModelAbCompareResult) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        _render_table_plain(result)
        return

    console = Console()
    table = Table(title="AB Model Cost Comparison", show_footer=True)

    table.add_column("Model", style="bold", footer="SAVINGS vs most expensive")
    table.add_column("Tokens", justify="right", footer="")
    table.add_column("Cost", justify="right", footer="")
    table.add_column("Time", justify="right", footer="")
    table.add_column("Quality", footer="")

    savings = _calculate_savings(result)

    for row in result.comparison:
        tokens = f"{row.total_tokens:,}" if not row.error else "—"
        cost = f"${row.cost_usd:.4f}" if not row.error else "error"
        latency = f"{row.latency_ms / 1000:.1f}s" if not row.error else "—"
        quality = row.quality or ("—" if not row.error else row.error[:40])
        style = "red" if row.error else ("green" if row.cost_usd == 0.0 else "")
        table.add_row(row.display_name, tokens, cost, latency, quality, style=style)

    # Patch footer for savings column
    table.columns[2].footer = f"${savings:.4f}"

    console.print(table)

    if result.models_skipped:
        console.print(f"[dim]Skipped: {', '.join(result.models_skipped)}[/dim]")
    console.print(f"[dim]Status: {result.status}[/dim]")


def _render_table_plain(result: ModelAbCompareResult) -> None:
    """Fallback plain-text table when rich is not installed."""
    header = f"{'Model':<24} {'Tokens':>8} {'Cost':>10} {'Time':>7} {'Quality':<10}"
    click.echo(header)
    click.echo("-" * len(header))
    for row in result.comparison:
        tokens = f"{row.total_tokens:,}" if not row.error else "—"
        cost = f"${row.cost_usd:.4f}" if not row.error else "error"
        latency = f"{row.latency_ms / 1000:.1f}s" if not row.error else "—"
        quality = row.quality or ("—" if not row.error else row.error[:30])
        click.echo(
            f"{row.display_name:<24} {tokens:>8} {cost:>10} {latency:>7} {quality:<10}"
        )
    click.echo("-" * len(header))
    savings = _calculate_savings(result)
    click.echo(f"{'SAVINGS vs most expensive':<24} {'':>8} ${savings:.4f}")
    if result.models_skipped:
        click.echo(f"Skipped: {', '.join(result.models_skipped)}")


def _render_json(result: ModelAbCompareResult) -> None:
    click.echo(json.dumps(result.model_dump(), indent=2))


def _run(
    task: str,
    models: list[str],
    system_prompt: str | None,
    quality_check: bool,
    output: str,
) -> int:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        HandlerAbCompareOrchestrator,
    )
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
        ModelAbCompareStart,
    )

    command = ModelAbCompareStart(
        task=task,
        models=models,
        correlation_id=str(uuid.uuid4()),
        system_prompt=system_prompt,
        quality_check=quality_check,
    )

    handler = HandlerAbCompareOrchestrator()
    result = asyncio.run(handler.handle(command))

    if output == "json":
        _render_json(result)
    else:
        _render_table(result)

    return 0 if result.status in ("COMPLETED", "PARTIAL") else 1


@click.command()
@click.option("--task", required=True, help="Coding task to run through models.")
@click.option(
    "--models",
    default="all",
    show_default=True,
    help="Comma-separated model IDs, or 'all'.",
)
@click.option("--system-prompt", default=None, help="Optional system prompt override.")
@click.option(
    "--quality-check", is_flag=True, default=False, help="Run ruff on code output."
)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
def main(
    task: str,
    models: str,
    system_prompt: str | None,
    quality_check: bool,
    output: str,
) -> None:
    """Run a task through multiple LLM models and compare cost, speed, and quality."""
    if models.strip().lower() == "all":
        model_list = ["all"]
    else:
        model_list = [
            model for model in (m.strip() for m in models.split(",")) if model
        ]
        if not model_list:
            raise click.BadParameter(
                "At least one model ID is required.", param_hint="--models"
            )
    sys.exit(_run(task, model_list, system_prompt, quality_check, output))
