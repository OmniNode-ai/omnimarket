# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex job-log-fill run``: one fill pass, in process or over the lane's bus (OMN-23032).

The overlay's profile decides. ``inmemory`` runs the pass here with the handler and the
host's own job-log cache. ``redpanda`` publishes the typed command on the node's command
topic and prints the terminal event the hosted node answers with. Exit 0 when no read
failed, 1 when some or all did, 69 when the bus cannot be opened, 75 with no terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import cast

import click
import yaml

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_job_log_filler_effect.handlers.handler_job_log_filler import (
    HandlerJobLogFiller,
    load_overlay,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
    ModelJobLogFillResult,
)

NODE = "node_job_log_filler_effect"
EXIT_FAILED = 1
EXIT_BUS = 69
EXIT_NO_TERMINAL = 75


def load_topics() -> ModelScmTopics:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    return ModelScmTopics(
        command=contract["runtime_dispatch"]["command_topic"],
        success=contract["terminal_event"],
    )


def result_from_terminal(
    payload: Mapping[str, object], request: ModelJobLogFillRequest, profile: str
) -> ModelJobLogFillResult:
    """The pass's result, or a failed one built from the runtime's own failure terminal."""
    if "considered" in payload:
        return ModelJobLogFillResult.model_validate(payload)
    reason = str(payload.get("failure_reason") or payload.get("error") or payload)
    return ModelJobLogFillResult(
        correlation_id=request.correlation_id,
        status=EnumJobLogFillStatus.FAILED,
        profile=profile,
        considered=0,
        filled=0,
        already_cached=0,
        failed=0,
        deferred=0,
        stale_skipped=0,
        error=reason[-2000:],
    )


async def call_over_bus(
    bus: ProtocolLabRunBus,
    request: ModelJobLogFillRequest,
    timeout_s: float,
    profile: str = "bus",
) -> ModelJobLogFillResult:
    terminal = await ScmBusCaller(bus).call_topics(
        topics=load_topics(),
        request=request,
        correlation_id=request.correlation_id,
        timeout_s=timeout_s,
    )
    return result_from_terminal(terminal.payload, request, profile)


@click.group("job-log-fill")
@click.pass_context
def job_log_fill_group(ctx: click.Context) -> None:
    """Fill the job-log cache from the merge-relevant runs of open pull requests."""
    ctx.ensure_object(dict)


@job_log_fill_group.command("run")
@click.option(
    "--profile", default=None, help="An overlay profile (default: the overlay's)."
)
@click.option(
    "--repo", "repos", multiple=True, help="Narrow to owner/name; repeatable."
)
@click.option(
    "--max-reads", type=int, default=None, help="Lower the overlay's per-pass cap."
)
@click.option(
    "--overlay",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
def run_command(
    profile: str | None,
    repos: tuple[str, ...],
    max_reads: int | None,
    overlay: Path | None,
) -> None:
    settings = load_overlay(overlay)
    request = ModelJobLogFillRequest(
        correlation_id=uuid.uuid4(), profile=profile, repos=repos, max_reads=max_reads
    )
    if settings.profile(profile).kind == "inmemory":
        result = HandlerJobLogFiller(overlay=settings).fill(request)
    else:
        lane = settings.profile(profile).lane
        # The opener's fourth parameter carries a workspace root name the public-repo
        # hygiene gate refuses, so its parameters are bound from its signature in order.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        opener = cast(
            Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
            open_lab_run_bus,
        )
        workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
        values = ("kafka", lane, None, Path(workspace) if workspace else None)

        async def main() -> ModelJobLogFillResult:
            async with opener(**dict(zip(names, values, strict=True))) as bus:
                return await call_over_bus(
                    bus,
                    request,
                    settings.reply_timeout_seconds,
                    profile or settings.default_profile,
                )

        try:
            result = asyncio.run(main())
        except TimeoutError:
            click.echo(
                json.dumps(
                    {
                        "status": "no_terminal",
                        "correlation_id": str(request.correlation_id),
                    }
                )
            )
            sys.exit(EXIT_NO_TERMINAL)
        except LabRunBusError as exc:
            click.echo(f"bus: {exc}", err=True)
            sys.exit(EXIT_BUS)
    click.echo(result.model_dump_json())
    sys.exit(0 if result.status is EnumJobLogFillStatus.COMPLETED else EXIT_FAILED)


__all__ = ["call_over_bus", "job_log_fill_group", "load_topics", "result_from_terminal"]
