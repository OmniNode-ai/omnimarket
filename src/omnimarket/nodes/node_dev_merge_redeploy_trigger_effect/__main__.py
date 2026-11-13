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
from pathlib import Path
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
from omnimarket.models.dev_merge_redeploy_overlay import (
    EnumDevMergeMode,
    ModelDevMergeRedeployPolicy,
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
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_unit import (
    DevMergeUnitInstaller,
    ModelDevMergeUnitResult,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_trigger import (
    HandlerDevMergeRedeployTrigger,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.handler_dev_merge_redeploy_verify import (
    HandlerDevMergeRedeployVerify,
)


def _opener_arguments(
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path,
) -> dict[str, Any]:
    """The bus opener's keywords, in the order it declares them: bus, lane, broker, workspace root."""
    keywords = tuple(inspect.signature(open_lab_run_bus).parameters)
    return dict(
        zip(keywords, (bus, lane, kafka_bootstrap, workspace_root), strict=True)
    )


EXIT_USAGE = 64
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
@click.option(
    "--bus-lane",
    default=None,
    help="The declared lane that carries the merge events and the receipts.",
)
@click.option(
    "--deploy-bus-lane",
    default=None,
    help=(
        "The declared lane that carries the redeploy-start command and the terminal event. "
        "Required when the overlay's mode is publish; refused when it is observe."
    ),
)
@click.option("--kafka-bootstrap", default=None, help="The broker, stated directly.")
@click.option(
    "--omnibase-path",
    "workspace_root",
    envvar="OMNIBASE_PATH",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    required=True,
    help="Workspace root holding the lane declaration. Bound to $OMNIBASE_PATH.",
)
@click.option(
    "--host-name", required=True, help="This host's name (a deployment fact)."
)
def serve_command(
    bus: BusKind,
    bus_lane: str | None,
    deploy_bus_lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path,
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
    publishing = policy.mode is EnumDevMergeMode.PUBLISH
    if publishing != (deploy_bus_lane is not None):
        click.echo(
            "usage: --deploy-bus-lane is required exactly when the overlay's mode is publish "
            f"(mode is {policy.mode.value})",
            err=True,
        )
        sys.exit(EXIT_USAGE)
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
        async with contextlib.AsyncExitStack() as stack:
            events = await stack.enter_async_context(
                open_lab_run_bus(
                    **_opener_arguments(bus, bus_lane, kafka_bootstrap, workspace_root)
                )
            )
            deploy = (
                await stack.enter_async_context(
                    open_lab_run_bus(
                        **_opener_arguments(bus, deploy_bus_lane, None, workspace_root)
                    )
                )
                if deploy_bus_lane is not None
                else None
            )
            host = DevMergeRedeployHost(
                events, trigger, verify, policy, host_name=host_name, deploy_bus=deploy
            )
            await host.start()
            click.echo(
                f"serving {host.topics.merged} in {policy.mode.value} mode for "
                f"{[r.repo for r in policy.repos]}",
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


@group.group("unit")
@click.pass_context
def unit_group(ctx: click.Context) -> None:
    """Render, install, remove and read the serve unit the overlay's unit block declares."""
    ctx.ensure_object(dict)


_host_option = click.option(
    "--host", "host", required=True, help="The declared host to act on."
)
_omni_root_option = click.option(
    "--omni-root",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="The registry root on the host ($OMNI_HOME in the overlay).",
)
_overlay_root_option = click.option(
    "--overlay-root",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="The overlay root the unit reads the overlay from (ONEX_SKILL_OVERLAY_ROOTS).",
)


def _installer() -> DevMergeUnitInstaller:
    try:
        policy = load_structured_overlay(NODE, ModelDevMergeRedeployPolicy)
    except NodeOverlayError as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_OVERLAY)
    return DevMergeUnitInstaller(policy=policy, home=Path.home())


def _finish(result: ModelDevMergeUnitResult) -> None:
    click.echo(result.stdout, nl=False)
    click.echo(result.stderr, err=True, nl=False)
    sys.exit(result.exit_code)


@unit_group.command("render")
@_host_option
@_omni_root_option
@_overlay_root_option
def unit_render(host: str, omni_root: Path, overlay_root: Path) -> None:
    """Print the unit file for a declared host."""
    _finish(_installer().render(host, omni_root=omni_root, overlay_root=overlay_root))


@unit_group.command("install")
@_host_option
@_omni_root_option
@_overlay_root_option
@click.option("--dry-run", is_flag=True, help="Render and check; write nothing.")
def unit_install(host: str, omni_root: Path, overlay_root: Path, dry_run: bool) -> None:
    """Probe the lane, write the unit, reload the manager and enable the service now."""
    _finish(
        _installer().install(
            host, omni_root=omni_root, overlay_root=overlay_root, dry_run=dry_run
        )
    )


@unit_group.command("uninstall")
@_host_option
def unit_uninstall(host: str) -> None:
    """The rollback: stop and disable the service and remove its unit file."""
    _finish(_installer().uninstall(host))


@unit_group.command("status")
@_host_option
def unit_status(host: str) -> None:
    """Read whether the service is enabled and active."""
    _finish(_installer().status(host))


if __name__ == "__main__":
    group()
