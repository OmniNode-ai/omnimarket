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
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import click
import yaml
from omnibase_core.models.ticket.model_ticket_draft import ModelTicketDraft
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates
from omnibase_infra.errors import InfraConnectionError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.models.ticket_write_io import (
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


_LANE_DECLARATION_FIX = (
    "export ONEX_WORKSPACE_CONFIG_ROOT=<config root holding config/lab/ci_bus_lanes.yaml> "
    "or pass --omnibase-path <workspace root>"
)


def _bus_unavailable_answer(
    correlation_id: UUID, exc: LabRunBusError | InfraConnectionError
) -> dict[str, str]:
    """The bus_unavailable terminal, naming why the bus could not be opened.

    A bus-opener refusal carries a message that names fields, files and
    variables only (never a credential), and a missing lane declaration is the
    one cause a host fixes itself, so its remedy is stated. A broker connection
    error keeps to its type name: its text can carry connection details.
    """
    answer = {"status": "bus_unavailable", "correlation_id": str(correlation_id)}
    if isinstance(exc, LabRunBusError):
        answer["detail"] = str(exc)
        if "ci_bus_lanes.yaml" in str(exc):
            answer["fix"] = _LANE_DECLARATION_FIX
    else:
        answer["detail"] = type(exc).__name__
    return answer


# A Done transition runs the done gate's verifier on the consumer, bounded at 300 s
# (done_write_verify_seconds); its terminal, a completion or a refusal, comes after
# that run. The wait outlasts it, or the caller reads no_terminal for a refusal.
_DEFAULT_WAIT_S = {"create": 60.0, "read": 60.0, "comment": 60.0, "transition": 330.0}


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
        timeout_s: float | None,
        **fields: object,
    ) -> None:
        if timeout_s is None:
            timeout_s = _DEFAULT_WAIT_S[operation]
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
        draft_path = fields.pop("draft_file", None)
        draft_id = fields.pop("draft_id", None)
        if draft_path is not None:
            path = Path(str(draft_path))
            try:
                draft = ModelTicketDraft.model_validate(
                    load_yaml_mapping_no_duplicates(
                        path.read_text(encoding="utf-8"), source=str(path)
                    )
                )
            except (OSError, ValueError, yaml.YAMLError) as exc:
                raise click.UsageError(f"invalid --draft file: {exc}") from exc
            fields["draft"] = draft.model_dump(mode="json")
            if draft_id is None:
                # Identical typed files retry the same draft across CLI invocations.
                # An explicit UUID can name an independently submitted copy.
                canonical = json.dumps(fields["draft"], sort_keys=True)
                draft_id = uuid5(NAMESPACE_URL, f"onex:ticket-draft:{canonical}")
        if draft_id is not None:
            fields["draft_id"] = str(draft_id)
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
        except (LabRunBusError, InfraConnectionError) as exc:
            click.echo(
                json.dumps(
                    _bus_unavailable_answer(correlation_id, exc), sort_keys=False
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
        click.option("--timeout-s", default=None, type=click.FloatRange(min=0.1)),
        click.option("--dry-run", is_flag=True),
    ):
        decorated = option(decorated)
    if operation == "create":
        for option in (
            click.option("--title", help="Title for a free-text create."),
            click.option(
                "--draft",
                "draft_file",
                type=click.Path(exists=True, dir_okay=False),
                help="YAML file containing a typed ticket draft.",
            ),
            click.option(
                "--draft-id",
                type=click.UUID,
                help="Stable UUID to reuse on retry. Defaults to a UUID derived from the draft file.",
            ),
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
