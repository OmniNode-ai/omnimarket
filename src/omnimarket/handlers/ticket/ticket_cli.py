# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex ticket``: guarded Linear operations over contract-declared bus topics.

Exit codes match SCM: 0 completed, 1 failed terminal, 2 usage, 69 bus
unavailable, 75 no terminal (unknown outcome; read before retrying).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from importlib import resources
from pathlib import Path
from uuid import uuid4

import click
import yaml
from omnibase_infra.errors import InfraConnectionError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteRequest,
)


def load_ticket_topics() -> ModelScmTopics:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_linear_ticket_write_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=dispatch["command_topic"], **dispatch["terminal_events"]
    )


@click.group("ticket")
@click.pass_context
def ticket_group(ctx: click.Context) -> None:
    """Create, read, comment on or transition Linear tickets over the event bus."""
    ctx.ensure_object(dict)


def _command(operation: str) -> click.Command:
    def command(
        lane: str,
        run: str | None,
        idempotency_key: str | None,
        timeout_s: float,
        **fields: object,
    ) -> None:
        bus_opts = {
            name: fields.pop(name)
            for name in ("omnibase_path", "bus", "bus_lane", "kafka_bootstrap")
        }
        # read_bytes preserves CRLF and the caller's exact UTF-8 description/body.
        for name in ("description", "body"):
            path = fields.pop(f"{name}_file", None)
            if path is not None:
                if fields.get(name):
                    raise click.UsageError(f"give --{name} or --{name}-file, not both")
                fields[name] = Path(str(path)).read_bytes().decode("utf-8")
        fields = {name: value for name, value in fields.items() if value is not None}
        identity = {
            "operation": operation,
            "requested_by_lane": lane,
            "requested_by_run": run,
            **fields,
        }
        key = (
            idempotency_key
            or hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        )
        correlation_id = uuid4()
        try:
            request = ModelTicketWriteRequest.model_validate(
                {**identity, "idempotency_key": key, "correlation_id": correlation_id}
            )
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc

        async def call() -> ModelScmTerminal:
            async with _open_bus(bus_opts) as opened:
                return await ScmBusCaller(opened).call_topics(
                    topics=load_ticket_topics(),
                    request=request,
                    correlation_id=correlation_id,
                    timeout_s=timeout_s,
                )

        try:
            terminal = asyncio.run(call())
        except TimeoutError:
            click.echo(
                json.dumps(
                    {
                        "status": "no_terminal",
                        "correlation_id": str(correlation_id),
                        "idempotency_key": key,
                    }
                )
            )
            sys.exit(75)
        except (LabRunBusError, InfraConnectionError):
            click.echo(
                json.dumps(
                    {"status": "bus_unavailable", "correlation_id": str(correlation_id)}
                )
            )
            sys.exit(69)
        click.echo(terminal.model_dump_json())
        sys.exit(1 if terminal.failed else 0)

    decorated = _with_bus_options(command)
    for option in (
        click.option("--lane", required=True, envvar="ONEX_LANE"),
        click.option("--run", default=None, envvar="ONEX_RUN_ID"),
        click.option("--idempotency-key", default=None),
        click.option("--timeout-s", default=60.0, type=click.FloatRange(min=0.1)),
        click.option("--dry-run", is_flag=True),
    ):
        decorated = option(decorated)
    if operation == "create":
        for option in (
            click.option("--title", required=True),
            click.option("--description", default=""),
            click.option(
                "--description-file", type=click.Path(exists=True, dir_okay=False)
            ),
            click.option("--parent", default=None),
            click.option("--repo", default=None),
            click.option("--team", default="Omninode"),
            click.option("--pillar", default=None),
        ):
            decorated = option(decorated)
    else:
        decorated = click.argument("ticket_id")(decorated)
    if operation == "comment":
        decorated = click.option("--body", default="")(decorated)
        decorated = click.option(
            "--body-file", type=click.Path(exists=True, dir_okay=False)
        )(decorated)
    if operation == "transition":
        decorated = click.option("--state", required=True)(decorated)
    return click.command(operation)(decorated)


for _operation in ("create", "read", "comment", "transition"):
    ticket_group.add_command(_command(_operation))


if __name__ == "__main__":
    ticket_group()
