# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm rerun-request``: ask the landing workflow to re-run one failed job (OMN-23830).

The command publishes a ``ModelLandingRerunRequest`` to the tick orchestrator's
contract-declared rerun topics and waits for the answer: completed for a re-run or a
duplicate, failed for limit_reached, head_moved or job_not_failed. Any principal may
ask; the tick answers the request, never the requester's lane.

Exit codes match SCM: 0 completed, 1 failed terminal, 2 usage, 69 bus unavailable,
75 no terminal (unknown outcome; read before retrying).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from importlib import resources
from pathlib import Path
from uuid import uuid4

import click
import yaml
from omnibase_infra.errors import InfraConnectionError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics

_TARGET = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$"
)


def load_rerun_request_topics() -> ModelScmTopics:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_pr_landing_tick_orchestrator")
        .joinpath("contract.yaml")
        .read_text()
    )
    rerun = contract["rerun_request"]
    return ModelScmTopics(
        command=rerun["command_topic"],
        success=rerun["completed_topic"],
        failure=rerun["failed_topic"],
    )


def rerun_request_command(
    open_bus: Callable[
        [dict[str, object]], AbstractAsyncContextManager[ProtocolLabRunBus]
    ],
    watcher_head: Callable[[str, int | None, str | None], str],
) -> click.Command:
    """The command, with the bus opener and the watcher-head read injected.

    scm_cli imports this module, so it hands over its own ``_open_bus`` and
    ``_watcher_head`` instead of this module importing them back.
    """

    def command(
        omnibase_path: Path | None,
        bus: str,
        bus_lane: str | None,
        kafka_bootstrap: str | None,
        target: str,
        job: str,
        reason: str,
        head_sha: str | None,
        watcher_state: str | None,
        lane: str,
        timeout_s: float,
    ) -> None:
        match = _TARGET.fullmatch(target)
        if match is None:
            raise click.UsageError(f"target {target!r} is not OWNER/REPO#NUMBER")
        repo = match.group("repo")
        number = int(match.group("number"))
        correlation_id = uuid4()
        try:
            request = ModelLandingRerunRequest(
                correlation_id=correlation_id,
                pr=f"{repo}#{number}",
                head_sha=head_sha
                if head_sha is not None
                else watcher_head(repo, number, watcher_state),
                job=job,
                reason=reason,
                requester=lane,
            )
        except ValidationError as exc:
            raise click.UsageError(str(exc)) from exc
        bus_opts: dict[str, object] = {
            "omnibase_path": omnibase_path,
            "bus": bus,
            "bus_lane": bus_lane,
            "kafka_bootstrap": kafka_bootstrap,
        }

        async def call() -> ModelScmTerminal:
            async with open_bus(bus_opts) as opened:
                return await ScmBusCaller(opened).call_topics(
                    topics=load_rerun_request_topics(),
                    request=request,
                    correlation_id=correlation_id,
                    timeout_s=timeout_s,
                )

        try:
            terminal = asyncio.run(call())
        except TimeoutError:
            click.echo(
                json.dumps(
                    {"status": "no_terminal", "correlation_id": str(correlation_id)}
                )
            )
            sys.exit(75)
        except (LabRunBusError, InfraConnectionError) as exc:
            # imported here: ticket_cli imports scm_cli, which imports this module
            from omnimarket.handlers.ticket.ticket_cli import _bus_unavailable_answer

            click.echo(
                json.dumps(
                    _bus_unavailable_answer(correlation_id, exc), sort_keys=False
                )
            )
            sys.exit(69)
        click.echo(terminal.model_dump_json())
        sys.exit(1 if terminal.failed else 0)

    decorated = _with_bus_options(command)
    decorated = click.argument("target")(decorated)
    for option in (
        click.option("--job", required=True, help="The failed check or job name."),
        click.option(
            "--reason",
            required=True,
            help="Why a re-run is right, in the requester's words; recorded with the answer.",
        ),
        click.option(
            "--head-sha",
            default=None,
            help="The head to re-run (default: the pull request's head in the watcher state).",
        ),
        click.option("--watcher-state", type=click.Path(dir_okay=False), default=None),
        click.option(
            "--lane", required=True, envvar="ONEX_LANE", help="The asking lane."
        ),
        click.option("--timeout-s", default=60.0, type=click.FloatRange(min=0.1)),
    ):
        decorated = option(decorated)
    return click.command(
        "rerun-request",
        help=(
            "Ask the landing workflow to re-run one failed job on a pull request's "
            "current head; any principal may ask."
        ),
    )(decorated)


__all__: list[str] = ["load_rerun_request_topics", "rerun_request_command"]
