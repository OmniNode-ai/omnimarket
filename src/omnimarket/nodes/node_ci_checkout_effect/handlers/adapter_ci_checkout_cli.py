# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex ci checkout``: guarded CI checkout over contract-declared bus topics.

Exit codes: 0 checked out, 1 not served, 69 bus unavailable, 75 no terminal.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

import click
import yaml
from omnibase_infra.errors import InfraConnectionError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_materialize import (
    CheckoutMaterializeError,
    materialize,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
)

EXIT_NOT_SERVED = 1
EXIT_BUS = 69
EXIT_NO_TERMINAL = 75


def load_topics() -> ModelScmTopics:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_ci_checkout_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    dispatch = contract["runtime_dispatch"]
    return ModelScmTopics(
        command=dispatch["command_topic"],
        success=dispatch["terminal_events"]["success"],
        failure=dispatch["terminal_events"]["failure"],
    )


def result_from_terminal(
    payload: dict[str, Any], request: ModelCiCheckoutRequest
) -> ModelCiCheckoutResult:
    """Parse a terminal payload into a result, or build a failed one."""
    if "status" in payload and "repo" in payload:
        return ModelCiCheckoutResult.model_validate(payload)
    reason = str(payload.get("failure_reason") or payload.get("error") or payload)
    return ModelCiCheckoutResult(
        correlation_id=request.correlation_id,
        host=request.host,
        status=EnumCiCheckoutStatus.FAILED,
        repo=request.repo,
        sha=request.sha,
        source_name=None,
        serve_url=None,
        upstream_fetches=0,
        detail=reason[-2000:],
    )


@click.group("ci")
@click.pass_context
def ci_group(ctx: click.Context) -> None:
    """CI operations over the event bus."""
    ctx.ensure_object(dict)


def _answer(payload: dict[str, object]) -> None:
    click.echo(json.dumps(payload, sort_keys=False))


def _checkout(
    *,
    repo: str,
    sha: str,
    dest: str,
    ref: str | None,
    lane: str,
    host: str,
    timeout_s: float,
    **bus_fields: Any,
) -> None:
    bus_opts = {
        name: bus_fields.pop(name)
        for name in ("omnibase_path", "bus", "bus_lane", "kafka_bootstrap")
    }
    correlation_id = uuid.uuid4()
    try:
        request = ModelCiCheckoutRequest(
            correlation_id=correlation_id,
            repo=repo,
            sha=sha,
            ref=ref,
            requested_by_lane=lane,
            host=host,
        )
    except ValidationError as exc:
        raise click.UsageError(str(exc)) from exc

    async def call() -> ModelScmTerminal:
        async with _open_bus(bus_opts) as opened:
            return await ScmBusCaller(opened).call_topics(
                topics=load_topics(),
                request=request,
                correlation_id=correlation_id,
                timeout_s=timeout_s,
            )

    started = time.monotonic()
    try:
        terminal = asyncio.run(call())
    except TimeoutError:
        _answer({"status": "no_terminal", "correlation_id": str(correlation_id)})
        sys.exit(EXIT_NO_TERMINAL)
    except (LabRunBusError, InfraConnectionError) as exc:
        _answer(
            {
                "status": "bus_unavailable",
                "correlation_id": str(correlation_id),
                "detail": type(exc).__name__,
            }
        )
        sys.exit(EXIT_BUS)
    seconds_bus = time.monotonic() - started

    result = result_from_terminal(terminal.payload, request)
    if result.status not in (
        EnumCiCheckoutStatus.SERVED,
        EnumCiCheckoutStatus.SYNCED_ON_MISS,
    ):
        _answer(
            {
                "status": result.status.value,
                "repo": result.repo,
                "sha": result.sha,
                "host": result.host,
                "detail": result.detail,
            }
        )
        sys.exit(EXIT_NOT_SERVED)

    started = time.monotonic()
    try:
        materialize(result, Path(dest), git_timeout_seconds=timeout_s)
    except (CheckoutMaterializeError, subprocess.TimeoutExpired) as exc:
        _answer(
            {
                "status": "materialize_failed",
                "repo": result.repo,
                "sha": result.sha,
                "source_name": result.source_name,
                "detail": str(exc)
                if isinstance(exc, CheckoutMaterializeError)
                else "timeout",
            }
        )
        sys.exit(EXIT_NOT_SERVED)
    _answer(
        {
            "status": result.status.value,
            "repo": result.repo,
            "sha": result.sha,
            "host": result.host,
            "source_name": result.source_name,
            "upstream_fetches": result.upstream_fetches,
            "dest": dest,
            "seconds_bus": round(seconds_bus, 3),
            "seconds_materialize": round(time.monotonic() - started, 3),
        }
    )
    sys.exit(0)


def _checkout_command() -> click.Command:
    decorated = _with_bus_options(_checkout)
    for option in (
        click.option("--repo", required=True, help="owner/name of the repository"),
        click.option("--sha", required=True, help="40-hex commit sha"),
        click.option(
            "--dest", required=True, help="directory to check the tree out into"
        ),
        click.option(
            "--ref", default=None, help="branch or head ref hint for a sync on a miss"
        ),
        click.option("--lane", required=True, envvar="ONEX_LANE"),
        click.option(
            "--host",
            required=True,
            envvar="ONEX_CI_CHECKOUT_HOST",
            help="The host whose clones serve the checkout (the runner's own host).",
        ),
        click.option("--timeout-s", default=60.0, type=click.FloatRange(min=0.1)),
    ):
        decorated = option(decorated)
    return click.command("checkout")(decorated)


ci_group.add_command(_checkout_command())


if __name__ == "__main__":
    ci_group()
