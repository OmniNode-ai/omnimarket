# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_merge_redeploy_trigger_effect serve`` (OMN-22478, R2).

Serves dev merges on this lab host until SIGTERM. The policy is the node overlay; the bus is
given by the same options the other lab serve processes take. Exits 69 when the bus is
unreachable and 78 when the overlay is absent or invalid.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import signal
import sys
from typing import Any

import click

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_git import (
    DeployPathClassifier,
    GitBuildContextRef,
    GitMergeFiles,
    ProvenanceLaneRevision,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_host import (
    NODE,
    DevMergeRedeployHost,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_trigger import (
    HandlerDevMergeRedeployTrigger,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_verify import (
    HandlerDevMergeRedeployVerify,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    ModelDevMergeRedeployPolicy,
)


def _opener_arguments(
    bus: BusKind, bus_lane: str | None, kafka_bootstrap: str | None
) -> dict[str, Any]:
    """The bus opener's keywords, in the order it declares them.

    The opener takes the bus, the lane, the broker and a workspace root. This process names no
    workspace root: the lane declaration is found the way the opener finds it when it is given
    none, so the fourth value is ``None``.
    """
    keywords = tuple(inspect.signature(open_lab_run_bus).parameters)
    return dict(zip(keywords, (bus, bus_lane, kafka_bootstrap, None), strict=True))


EXIT_BUS = 69
EXIT_OVERLAY = 78


@click.group("dev-merge-redeploy")
@click.pass_context
def group(ctx: click.Context) -> None:
    """Redeploy and verify the dev lane when a configured repository merges to dev."""
    ctx.ensure_object(dict)


@group.command("serve")
@click.option(
    "--bus",
    type=click.Choice(["kafka", "inmemory"]),
    default="kafka",
    show_default=True,
)
@click.option("--bus-lane", default=None, help="The declared bus lane to use.")
@click.option("--kafka-bootstrap", default=None, help="The broker, stated directly.")
@click.option(
    "--host-name", required=True, help="This host's name (a deployment fact)."
)
def serve_command(
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    host_name: str,
) -> None:
    """Serve dev merges on this host until SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        policy = load_structured_overlay(NODE, ModelDevMergeRedeployPolicy)
    except NodeOverlayError as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_OVERLAY)
    probe = [str(part) for part in policy.lane_probe_argv]
    trigger = HandlerDevMergeRedeployTrigger(
        policy,
        files=GitMergeFiles(),
        classifier=DeployPathClassifier(policy.classifier_path),
        build_context=GitBuildContextRef(),
    )
    verify = HandlerDevMergeRedeployVerify(policy, lane=ProvenanceLaneRevision(probe))

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        async with open_lab_run_bus(
            **_opener_arguments(bus, bus_lane, kafka_bootstrap)
        ) as opened:
            host = DevMergeRedeployHost(
                opened, trigger, verify, policy, host_name=host_name
            )
            await host.start()
            click.echo(
                f"serving {host.topics.merged} for {[r.repo for r in policy.repos]}",
                err=True,
            )
            try:
                await stop.wait()
            finally:
                await host.stop()

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS)


if __name__ == "__main__":
    group()
