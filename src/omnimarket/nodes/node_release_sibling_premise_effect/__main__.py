# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_release_sibling_premise_effect read`` (OMN-24209).

Reads the dev lane's premise for one candidate revision of one repository: the receipts
R2 published, the clone's branch history and the pure compute node's decision, then one
verdict event on the bus and one verdict JSON line on stdout. Exits 0 when the verdict
state is ready, 3 for any other state, 65 for a typed read failure, 69 when the bus is
unreachable and 78 when the overlay is absent or invalid.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.events.sibling_premise_verdict import (
    ModelSiblingPremiseVerdict,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_git import (
    DeployPathClassifier,
    GitMergeFiles,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.adapter_branch_history import (
    GitBranchHistory,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.adapter_receipt_source import (
    open_receipt_source,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.handler_sibling_premise_read import (
    HandlerSiblingPremiseRead,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.lane_keywords import (
    lane_keywords,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.sink import (
    BusVerdictSink,
    InMemoryVerdictSink,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_command import (
    ModelSiblingPremiseReadCommand,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_failed import (
    SiblingPremiseReadError,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_topics import (
    load_topics,
)
from omnimarket.nodes.node_release_sibling_premise_effect.overlay import load_overlay

EXIT_NOT_READY = 3
EXIT_READ_FAILED = 65
EXIT_BUS = 69
EXIT_OVERLAY = 78


def _clock() -> datetime:
    """The wall clock the handler stamps its reads with."""
    return datetime.now(UTC)


@click.group("release-sibling-premise")
@click.pass_context
def group(ctx: click.Context) -> None:
    """Read whether the dev lane vendors a candidate revision, from the deploy agent's receipts."""
    ctx.ensure_object(dict)


@group.command("read")
@click.option(
    "--bus",
    type=click.Choice(["kafka", "inmemory"]),
    default="kafka",
    show_default=True,
)
@click.option(
    "--bus-lane",
    default=None,
    help="The declared lane that carries the receipts and the verdict.",
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
    "--publish/--no-publish",
    default=True,
    show_default=True,
    help=(
        "Publish the verdict event on the bus. --no-publish reads the receipts from the broker "
        "and keeps the verdict in memory (for a lane whose verdict topics are not provisioned yet)."
    ),
)
@click.option("--repo", required=True, help="The repository the candidate belongs to.")
@click.option(
    "--candidate-sha",
    default=None,
    help=(
        "The candidate revision; omitted, the newest runtime-relevant "
        "ancestor on the base branch."
    ),
)
def read_command(
    bus: BusKind,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path,
    publish: bool,
    repo: str,
    candidate_sha: str | None,
) -> None:
    """Read the sibling premise for one candidate revision and print one verdict."""
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_OVERLAY)
    history = GitBranchHistory(timeout=overlay.git_timeout_seconds)
    files = GitMergeFiles(timeout=overlay.git_timeout_seconds)
    classifier = DeployPathClassifier(
        overlay.classifier_path,
        git_timeout=overlay.git_timeout_seconds,
        timeout=overlay.classifier_timeout_seconds,
    )
    topics = load_topics()

    async def run() -> ModelSiblingPremiseVerdict:
        async with contextlib.AsyncExitStack() as stack:
            # One process dispatches to one lane: the bus opener binds the lane's client
            # transport first, and the receipt source reads inside that binding.
            sink: InMemoryVerdictSink | BusVerdictSink = InMemoryVerdictSink()
            if bus == "kafka":
                opened = await stack.enter_async_context(
                    open_lab_run_bus(
                        **lane_keywords(
                            open_lab_run_bus,
                            bus,
                            bus_lane,
                            kafka_bootstrap,
                            workspace_root,
                        )
                    )
                )
                if publish:
                    sink = BusVerdictSink(opened)
            receipts = await stack.enter_async_context(
                open_receipt_source(
                    bus=bus,
                    lane=bus_lane,
                    kafka_bootstrap=kafka_bootstrap,
                    workspace_root=workspace_root,
                )
            )
            read = HandlerSiblingPremiseRead(
                overlay,
                receipts=receipts,
                history=history,
                files=files,
                classifier=classifier,
                decide=HandlerSiblingPremise(),
                sink=sink,
                clock=_clock,
                topics=topics,
            )
            return await read.handle(
                ModelSiblingPremiseReadCommand(repo=repo, candidate_sha=candidate_sha)
            )
        raise AssertionError("unreachable: the read returns inside the exit stack")

    try:
        verdict = asyncio.run(run())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS)
    except SiblingPremiseReadError as exc:
        click.echo(json.dumps({"failed": exc.code.value, "message": str(exc)}))
        sys.exit(EXIT_READ_FAILED)
    click.echo(verdict.model_dump_json())
    sys.exit(0 if verdict.ready else EXIT_NOT_READY)


def main() -> None:
    """The module entry point."""
    group()


if __name__ == "__main__":
    main()
