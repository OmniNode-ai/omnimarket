# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_lab_job_submit_effect.

Usage:
    python -m omnimarket.nodes.node_lab_job_submit_effect submit --spec-file <file>
        [--bus kafka|inmemory] [--bus-lane <lane>] [--kafka-bootstrap <broker>]

The file is either the remote-lane runner's record ({"topic", "spec", ...}) or
a bare spec, validated as the canonical ModelLabJobSpec with nothing translated
or defaulted by this CLI. Exits 0 when published, 65 when refused (nothing
opened or sent), and 69 when the bus is unavailable or refuses the publish.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from omnibase_infra.errors import EventTopicAuthorizationError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.lab_job import ModelLabJobSpec
from omnimarket.nodes.node_lab_job_submit_effect import (
    HandlerLabJobSubmitEffect,
    ModelLabJobSubmitReceipt,
    load_lab_job_submitted_topic,
)

EXIT_REFUSED = 65
EXIT_BUS_UNAVAILABLE = 69


@click.group("lab-job")
@click.pass_context
def lab_job_group(ctx: click.Context) -> None:
    """Submit lab jobs to the supervisor over the declared bus."""
    ctx.ensure_object(dict)


@lab_job_group.command("submit")
@_with_bus_options
@click.option(
    "--spec-file", required=True, type=click.Path(dir_okay=False, allow_dash=True)
)
def submit_command(
    spec_file: str,
    omnibase_path: Path | None,
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
) -> None:
    """Validate a spec or runner record and publish one submitted command."""
    try:
        with click.open_file(spec_file, encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError("spec file must contain a JSON object")
        if "spec" in data:
            if "topic" in data and data["topic"] != load_lab_job_submitted_topic():
                raise ValueError(
                    "record topic does not match the submitted command topic"
                )
            data = data["spec"]
        spec = ModelLabJobSpec.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        click.echo(f"refused: {exc}", err=True)
        sys.exit(EXIT_REFUSED)

    async def publish() -> ModelLabJobSubmitReceipt:
        async with open_lab_run_bus(
            bus=bus,
            lane=bus_lane,
            kafka_bootstrap=kafka_bootstrap,
            omni_home=omnibase_path,
        ) as opened:
            return await HandlerLabJobSubmitEffect(opened).handle(spec)

    try:
        receipt = asyncio.run(publish())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS_UNAVAILABLE)
    except EventTopicAuthorizationError as exc:
        click.echo(f"bus: publish refused by the broker's ACLs: {exc}", err=True)
        sys.exit(EXIT_BUS_UNAVAILABLE)
    click.echo(receipt.model_dump_json())


def main() -> None:
    """Run the lab job submission CLI."""
    lab_job_group()


if __name__ == "__main__":
    main()
