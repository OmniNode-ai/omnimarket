# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex test-loop`` -- run the delegated test loop, and serve its focused runs
on a lab host (OMN-19458).

    # On the lab docker host: serve the focused-run command topic.
    onex test-loop serve-runs --lane dogfood

    # Anywhere: run one loop. Delegate calls go to the dev lane's deployed
    # orchestrator; each focused run goes over the bus to the lab host.
    onex test-loop run --request loop.json --lane dogfood --delegate-lane dev

    # Offline: the in-memory bus, the focused runs executed by this machine.
    onex test-loop run --request loop.json --bus inmemory --delegate-in-process

    # Run existing tests at one commit on the lab host, and print the summary.
    onex test-loop run-focused --repo OmniNode-ai/omnimarket --commit <sha> \
        --node-id tests/a/test_x.py --node-id tests/b/test_y.py --lane dogfood

``run-focused`` prints ONE line on stdout: each file's focused-run outcome and
the combined summary (``64 passed``), and exits 0 when every run completed with
exit 0, 3 when a run completed with failing tests, and 1 when a run could not
complete (an infrastructure error, or a host still busy after its retries).

``run`` prints ONE line on stdout, the loop's compact result, and exits 0 when
the loop ends accepted (accepted_call, accepted_mutation or
accepted_collection), 3 on any other loop status, and 1 when the loop could
not run at all. The loop receipt is
``<state root>/runs/<correlation id>/loop_receipt.json``.

WHY THE RUN GOES OVER THE BUS. Operator ruling 2026-09-25: the model-written
test executes only on a lab host that already has the sandbox rails, behind
its own command topic, and no runtime holds ssh credentials to that host or a
docker socket. ``serve-runs`` is that host's side; nothing else consumes the
command topic, and the shared runtimes do not attach the node (its contract
declares no ``event_bus`` block).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path

import click

from omnimarket.delegated_test_loop.lab_run_bus import (
    DEFAULT_MAX_COMMAND_AGE_SECONDS,
    FocusedRunHost,
    ProtocolFocusedRunHandler,
    ProtocolLabRunBus,
)
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBridge,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.delegated_test_loop.loop_ports import (
    IN_PROCESS_DELEGATE_FLAGS,
    DelegatedTestLoopPorts,
    deployed_lane_delegate_flags,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator import (
    EnumLoopStatus,
    HandlerDelegatedTestLoopOrchestrator,
    ModelDelegatedTestLoopRequest,
)
from omnimarket.nodes.node_focused_test_run_effect import (
    DEFAULT_ALLOWED_OWNERS,
    HandlerLabFocusedTestRunEffect,
)
from omnimarket.nodes.node_push_validation_effect import (
    EnumFocusedTestRunStatus,
    ModelFocusedTestRunRequest,
)
from omnimarket.nodes.node_pytest_failure_digest_compute import (
    ModelPytestRunReport,
    digest_pytest_run,
)

logger = logging.getLogger(__name__)

ACCEPTED_STATUSES: frozenset[EnumLoopStatus] = frozenset(
    {
        EnumLoopStatus.ACCEPTED_CALL,
        EnumLoopStatus.ACCEPTED_MUTATION,
        EnumLoopStatus.ACCEPTED_COLLECTION,
    }
)
EXIT_NOT_ACCEPTED = 3


def lab_handler(allowed_owners: frozenset[str]) -> ProtocolFocusedRunHandler:
    """The focused-run handler this machine runs. A seam for tests."""
    return HandlerLabFocusedTestRunEffect(allowed_owners=allowed_owners)


def delegate_runner() -> Callable[[list[str]], subprocess.CompletedProcess[str]] | None:
    """How each ``onex delegate`` argv is run; ``None`` runs it as a process.
    A seam for tests."""
    return None


def _omni_home() -> Path:
    try:
        return Path(os.environ["OMNI_HOME"])
    except KeyError as exc:
        raise click.ClickException(
            "OMNI_HOME is not set: it locates the onex wrapper and the lane declaration"
        ) from exc


def _opener(
    bus: BusKind, lane: str | None, kafka_bootstrap: str | None, omni_home: Path
) -> Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]:
    return partial(
        open_lab_run_bus,
        bus=bus,
        lane=lane,
        kafka_bootstrap=kafka_bootstrap,
        omni_home=omni_home,
    )


@click.group("test-loop")
def test_loop_group() -> None:
    """Run the delegated test loop; serve its focused runs on a lab host."""


@test_loop_group.command("run")
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The loop request (ModelDelegatedTestLoopRequest JSON).",
)
@click.option(
    "--new-correlation",
    is_flag=True,
    help="Mint a fresh correlation id instead of the request's own (a rerun).",
)
@click.option(
    "--state-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".onex_state"),
    show_default=True,
)
@click.option(
    "--source-clone",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="A clone holding the fixed ref, for the target excerpt. "
    "Default: $OMNI_HOME/<repository name>.",
)
@click.option(
    "--bus",
    type=click.Choice(["kafka", "inmemory"]),
    default="kafka",
    show_default=True,
    help="The bus the focused runs travel on.",
)
@click.option("--lane", default=None, help="The declared lane of the run bus.")
@click.option(
    "--kafka-bootstrap", default=None, help="The run bus broker, stated directly."
)
@click.option(
    "--delegate-lane",
    default="dev",
    show_default=True,
    help="The lane whose deployed orchestrator answers the delegate calls.",
)
@click.option(
    "--delegate-in-process",
    is_flag=True,
    help="Run each delegate call in this process instead (the first slice).",
)
@click.option(
    "--wait-slack",
    type=int,
    default=900,
    show_default=True,
    help="Seconds past a run's own timeout to wait for its terminal.",
)
def run_command(
    request_path: Path,
    new_correlation: bool,
    state_root: Path,
    source_clone: Path | None,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    delegate_lane: str,
    delegate_in_process: bool,
    wait_slack: int,
) -> None:
    """Run one delegated test loop and print its compact result."""
    omni_home = _omni_home()
    try:
        request = ModelDelegatedTestLoopRequest.model_validate_json(
            request_path.read_text()
        )
    except ValueError as exc:
        raise click.ClickException(f"unreadable loop request: {exc}") from exc
    if new_correlation:
        request = request.model_copy(update={"correlation_id": str(uuid.uuid4())})
    clone = source_clone or omni_home / request.repo.split("/", 1)[1]
    host_handler = lab_handler(DEFAULT_ALLOWED_OWNERS) if bus == "inmemory" else None
    delegate_flags = (
        IN_PROCESS_DELEGATE_FLAGS
        if delegate_in_process
        else deployed_lane_delegate_flags(delegate_lane)
    )
    try:
        with LabRunBridge(
            _opener(bus, lane, kafka_bootstrap, omni_home),
            host_handler=host_handler,
            wait_slack_seconds=wait_slack,
        ) as bridge:
            ports = DelegatedTestLoopPorts(
                onex=omni_home / "omnibase_infra" / "scripts" / "onex",
                state_root=state_root.resolve(),
                source_clone=clone.resolve(),
                test_path=request.test_path,
                run_focused=bridge.run,
                delegate_flags=delegate_flags,
                run_delegate=delegate_runner(),
            )
            result = HandlerDelegatedTestLoopOrchestrator(ports).run(request)
    except LabRunBusError as exc:
        raise click.ClickException(f"run bus: {exc}") from exc
    click.echo(json.dumps(result.model_dump(mode="json"), separators=(",", ":")))
    if result.status not in ACCEPTED_STATUSES:
        sys.exit(EXIT_NOT_ACCEPTED)


MAX_FOCUSED_NODE_IDS = 9
HOST_BUSY_TRIES = 6
HOST_BUSY_BACKOFF_SECONDS = 30


def summary_line(passed: int, failed: int, errors: int, skipped: int) -> str:
    """pytest's own summary vocabulary: ``64 passed``, ``1 failed, 3 passed``."""
    parts = [
        f"{count} {word}"
        for count, word in (
            (failed, "failed"),
            (passed, "passed"),
            (skipped, "skipped"),
            (errors, "errors" if errors != 1 else "error"),
        )
        if count
    ]
    return ", ".join(parts) or "no tests ran"


@test_loop_group.command("run-focused")
@click.option("--repo", required=True, help="owner/name of a public repository.")
@click.option("--commit", "commit_sha", required=True, help="The full 40-hex commit.")
@click.option(
    "--node-id",
    "node_ids",
    required=True,
    multiple=True,
    help="A test file or pytest node id under tests/ (repeatable, at most 9).",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=300,
    show_default=True,
    help="Seconds each run may take in its container.",
)
@click.option(
    "--state-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".onex_state"),
    show_default=True,
)
@click.option(
    "--bus",
    type=click.Choice(["kafka", "inmemory"]),
    default="kafka",
    show_default=True,
)
@click.option("--lane", default=None, help="The declared lane of the run bus.")
@click.option("--kafka-bootstrap", default=None, help="The broker, stated directly.")
@click.option(
    "--wait-slack",
    type=int,
    default=900,
    show_default=True,
    help="Seconds past a run's own timeout to wait for its terminal (an "
    "environment image build on the host counts against it).",
)
def run_focused_command(
    repo: str,
    commit_sha: str,
    node_ids: tuple[str, ...],
    timeout_seconds: int,
    state_root: Path,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    wait_slack: int,
) -> None:
    """Run existing tests at one commit on the lab host and print the summary."""
    if len(node_ids) > MAX_FOCUSED_NODE_IDS:
        raise click.ClickException(
            f"at most {MAX_FOCUSED_NODE_IDS} node ids per invocation"
        )
    omni_home = _omni_home()
    correlation_id = str(uuid.uuid4())
    try:
        requests = [
            ModelFocusedTestRunRequest(
                repo=repo,
                commit_sha=commit_sha,
                test_node_id=node_id,
                timeout_seconds=timeout_seconds,
                correlation_id=correlation_id,
                ref_role="fixed",
                attempt=index,
            )
            for index, node_id in enumerate(node_ids, start=1)
        ]
    except ValueError as exc:
        raise click.ClickException(f"invalid focused-run request: {exc}") from exc
    focused_dir = state_root.resolve() / "runs" / correlation_id / "focused"
    focused_dir.mkdir(parents=True, exist_ok=True)
    host_handler = lab_handler(DEFAULT_ALLOWED_OWNERS) if bus == "inmemory" else None
    runs: list[dict[str, object]] = []
    totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    exits: list[int] = []
    incomplete = False
    try:
        with LabRunBridge(
            _opener(bus, lane, kafka_bootstrap, omni_home),
            host_handler=host_handler,
            wait_slack_seconds=wait_slack,
        ) as bridge:
            for request in requests:
                receipt = bridge.run(request)
                for tries in range(1, HOST_BUSY_TRIES):
                    if receipt.status is not EnumFocusedTestRunStatus.HOST_BUSY:
                        break
                    busy_sleep(HOST_BUSY_BACKOFF_SECONDS * tries)
                    receipt = bridge.run(request)
                receipt_id = f"{correlation_id[:8]}-a{request.attempt}"
                (focused_dir / f"{receipt_id}.json").write_text(
                    json.dumps(receipt.model_dump(mode="json"), indent=1)
                )
                row: dict[str, object] = {
                    "node_id": request.test_node_id,
                    "receipt_id": receipt_id,
                    "status": receipt.status.value,
                    "exit_code": receipt.exit_code,
                    "host": receipt.host,
                }
                if receipt.status is EnumFocusedTestRunStatus.COMPLETED:
                    digest = digest_pytest_run(
                        ModelPytestRunReport(
                            junit_xml=receipt.junit_xml, exit_code=receipt.exit_code
                        )
                    )
                    counts = {
                        "passed": digest.tests
                        - digest.failures
                        - digest.errors
                        - digest.skipped,
                        "failed": digest.failures,
                        "errors": digest.errors,
                        "skipped": digest.skipped,
                    }
                    for key, value in counts.items():
                        totals[key] += value
                    row.update(outcome=digest.outcome.value, **counts)
                    if receipt.exit_code is not None:
                        exits.append(receipt.exit_code)
                else:
                    incomplete = True
                    row["detail"] = receipt.detail[:300]
                runs.append(row)
    except LabRunBusError as exc:
        raise click.ClickException(f"run bus: {exc}") from exc
    exit_code = max(exits) if exits and not incomplete else None
    click.echo(
        json.dumps(
            {
                "correlation_id": correlation_id,
                "repo": repo,
                "commit": commit_sha,
                "runs": runs,
                "summary": summary_line(**totals),
                "exit": exit_code,
            },
            separators=(",", ":"),
        )
    )
    if incomplete:
        sys.exit(1)
    if exit_code != 0:
        sys.exit(EXIT_NOT_ACCEPTED)


def busy_sleep(seconds: float) -> None:
    """Back off before retrying a run the host refused as busy. A seam for tests."""
    time.sleep(seconds)


@test_loop_group.command("serve-runs")
@click.option(
    "--bus",
    type=click.Choice(["kafka", "inmemory"]),
    default="kafka",
    show_default=True,
)
@click.option("--lane", default=None, help="The declared lane to serve on.")
@click.option("--kafka-bootstrap", default=None, help="The broker, stated directly.")
@click.option(
    "--allowed-owner",
    "allowed_owners",
    multiple=True,
    default=tuple(sorted(DEFAULT_ALLOWED_OWNERS)),
    show_default=True,
    help="A repository owner this host runs tests for (repeatable).",
)
@click.option(
    "--max-command-age",
    type=int,
    default=DEFAULT_MAX_COMMAND_AGE_SECONDS,
    show_default=True,
    help="Seconds after which a queued command is answered, not run.",
)
@click.option(
    "--max-commands",
    type=int,
    default=0,
    show_default=True,
    help="Stop after this many commands (0 serves until interrupted).",
)
def serve_runs_command(
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    allowed_owners: tuple[str, ...],
    max_command_age: int,
    max_commands: int,
) -> None:
    """Serve the focused-run command topic on this lab docker host."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    omni_home = Path(os.environ["OMNI_HOME"]) if "OMNI_HOME" in os.environ else None
    handler = lab_handler(frozenset(allowed_owners))
    try:
        asyncio.run(
            _serve(
                open_lab_run_bus(
                    bus=bus,
                    lane=lane,
                    kafka_bootstrap=kafka_bootstrap,
                    omni_home=omni_home,
                ),
                handler,
                max_command_age,
                max_commands,
            )
        )
    except LabRunBusError as exc:
        raise click.ClickException(f"run bus: {exc}") from exc


async def _serve(
    opened: contextlib.AbstractAsyncContextManager[ProtocolLabRunBus],
    handler: ProtocolFocusedRunHandler,
    max_command_age: int,
    max_commands: int,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with opened as bus:
        host = FocusedRunHost(
            bus,
            handler,
            max_command_age_seconds=max_command_age,
        )
        await host.start()
        try:
            while not stop.is_set():
                if max_commands and host.processed >= max_commands:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
        finally:
            await host.stop()


__all__ = [
    "ACCEPTED_STATUSES",
    "EXIT_NOT_ACCEPTED",
    "busy_sleep",
    "delegate_runner",
    "lab_handler",
    "run_command",
    "run_focused_command",
    "serve_runs_command",
    "summary_line",
    "test_loop_group",
]
