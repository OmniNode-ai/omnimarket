# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex trajectory-eval``: the hand publisher of this node's two commands.

    # One delegation run directory, attributed to the clone it was made for.
    onex trajectory-eval submit --run-dir <run dir> --mode meta --lane dev
    onex trajectory-eval submit --run-dir <run dir> --mode full --repo-dir <clone> --lane dev

    # A command written by hand, published exactly as the file holds it.
    onex trajectory-eval submit --from-json command.json --lane dev

    # One poll command per accepted receipt still pending in a captured outcome log.
    onex trajectory-eval poll --outcomes outcomes.jsonl --max 10 --lane dev

    # Print the command bytes, one per line, and open no bus.
    onex trajectory-eval submit --run-dir <run dir> --mode meta --dry-run

The commands are built from the node's own models and travel on the topics its
``contract.yaml`` routes to them. The bus is opened the way the delegated test
loop opens it: ``--lane`` or ``--kafka-bootstrap``, never an ambient broker
variable. The node paces itself; this publisher does not.

Exit codes: 0 published (or printed), 2 a usage error (a clone with no GitHub
``origin``, a malformed ``--from-json`` file), 3 the run directory holds a run
that did not succeed (nothing is published for it).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections.abc import Iterable
from importlib import resources
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast
from uuid import UUID

import click
import yaml
from pydantic import ValidationError

from omnimarket.cli.cli_test_loop import _opener as lab_run_bus_opener
from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for
from omnimarket.delegated_test_loop.lane_bus import BusKind, LabRunBusError
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationPoll,
    ModelTrajectoryEvaluationSubmit,
)

EXIT_RUN_FAILED = 3
DEFAULT_POLL_MAX = 20

_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<name>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
_SUCCESS_STATUS = "success"


class TrajectoryEvalUsageError(click.UsageError):
    """The input cannot name a command: exit 2, nothing published."""


class TrajectoryEvalRunFailedError(click.ClickException):
    """The run directory holds a run that did not succeed."""

    exit_code = EXIT_RUN_FAILED


class CommandTopics(NamedTuple):
    submit: str
    poll: str


def command_topics() -> CommandTopics:
    """The topics the node's ``handler_routing`` routes each command model to."""
    text = (
        resources.files("omnimarket.nodes.node_trajectory_evaluation_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    routes = yaml.safe_load(text)["handler_routing"]["handlers"]
    by_model = {route["event_model"]["name"]: route["topic"] for route in routes}
    return CommandTopics(
        submit=by_model[ModelTrajectoryEvaluationSubmit.__name__],
        poll=by_model[ModelTrajectoryEvaluationPoll.__name__],
    )


def resolve_repository(repo_dir: Path) -> str:
    """``owner/name`` of the GitHub ``origin`` of the clone at ``repo_dir``."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise TrajectoryEvalUsageError(f"git could not be run: {exc}") from exc
    if completed.returncode != 0:
        raise TrajectoryEvalUsageError(
            f"{repo_dir} is not a git clone with an origin remote: "
            + completed.stderr.strip()
        )
    url = completed.stdout.strip()
    match = _GITHUB_REMOTE.match(url)
    if match is None:
        raise TrajectoryEvalUsageError(
            f"origin of {repo_dir} is not a GitHub repository: {url}"
        )
    return f"{match['owner']}/{match['name']}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrajectoryEvalRunFailedError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TrajectoryEvalRunFailedError(f"{path} does not hold a JSON object")
    return cast("dict[str, Any]", loaded)


def _find_dict(value: object, key: str) -> dict[str, Any] | None:
    """The first object under ``key`` anywhere in ``value``, depth first."""
    if isinstance(value, dict):
        found = value.get(key)
        if isinstance(found, dict):
            return cast("dict[str, Any]", found)
        children: Iterable[object] = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        nested = _find_dict(child, key)
        if nested is not None:
            return nested
    return None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def submit_from_run_dir(
    run_dir: Path, mode: Literal["meta", "full"], repository: str
) -> ModelTrajectoryEvaluationSubmit:
    """The submit command for one ``onex delegate`` run directory.

    A run whose receipt is missing, whose status is not success, or whose
    route is unattributed raises :class:`TrajectoryEvalRunFailedError`.
    """
    receipt = _read_json(run_dir / "receipt.json")
    run = _read_json(run_dir / "run.json")
    status = receipt.get("status")
    if status != _SUCCESS_STATUS or receipt.get("route_attributed") is False:
        raise TrajectoryEvalRunFailedError(
            f"run {receipt.get('run_id')} did not succeed (status {status}); "
            "nothing published"
        )
    metrics = _find_dict(receipt.get("receipt"), "metrics") or {}
    result = _find_dict(receipt, "result") or {}
    quality = receipt.get("quality_score", result.get("quality_score"))
    gate = receipt.get("quality_gate_passed", result.get("quality_gate_passed"))
    texts: dict[str, str] = {}
    if mode == "full":
        try:
            response = (run_dir / "result.txt").read_text(encoding="utf-8")
        except OSError as exc:
            raise TrajectoryEvalRunFailedError(
                f"{run_dir / 'result.txt'} is unreadable: {exc}"
            ) from exc
        texts = {"prompt_text": str(run.get("prompt", "")), "response_text": response}
    try:
        return ModelTrajectoryEvaluationSubmit(
            correlation_id=receipt.get("correlation_id") or run.get("correlation_id"),
            work_unit_repository=repository,
            content_mode=mode,
            model_id=receipt.get("model"),
            provider=receipt.get("backend_id"),
            task_type=run.get("task_type"),
            prompt_tokens=_int_or_none(metrics.get("prompt_tokens")),
            completion_tokens=_int_or_none(metrics.get("completion_tokens")),
            latency_ms=_int_or_none(metrics.get("latency_ms")),
            quality_score=quality if isinstance(quality, int | float) else None,
            gate_result=(
                None if not isinstance(gate, bool) else "pass" if gate else "fail"
            ),
            **texts,
        )
    except ValidationError as exc:
        raise TrajectoryEvalRunFailedError(
            f"run directory {run_dir} names no usable correlation id: {exc}"
        ) from exc


def _payload_of(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return cast("dict[str, Any]", payload) if isinstance(payload, dict) else row


def pending_polls(outcomes: Path, limit: int) -> list[ModelTrajectoryEvaluationPoll]:
    """One poll command per receipt whose last outcome is still pending.

    ``outcomes`` holds one outcome per line, bare or inside its event envelope,
    in the order they were consumed. Refused and failed outcomes carry no
    receipt and are skipped.
    """
    state: dict[str, tuple[UUID, str | None, bool]] = {}
    try:
        lines = outcomes.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TrajectoryEvalUsageError(f"{outcomes} is unreadable: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise TrajectoryEvalUsageError(
                f"{outcomes}:{number} is not JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            continue
        payload = _payload_of(cast("dict[str, Any]", row))
        receipt_id = payload.get("backend_receipt_id")
        verdict_state = payload.get("verdict_state")
        if not isinstance(receipt_id, str) or verdict_state not in (
            "pending",
            "completed",
        ):
            continue
        try:
            correlation_id = UUID(str(payload.get("correlation_id")))
        except ValueError as exc:
            raise TrajectoryEvalUsageError(
                f"{outcomes}:{number} has no correlation id"
            ) from exc
        previous = state.get(receipt_id)
        mode = payload.get("content_mode")
        if not isinstance(mode, str):
            mode = previous[1] if previous is not None else None
        terminal = verdict_state == "completed" or (
            previous is not None and previous[2]
        )
        state[receipt_id] = (correlation_id, mode, terminal)
    polls = [
        ModelTrajectoryEvaluationPoll(
            correlation_id=correlation_id,
            content_mode=mode,
            backend_receipt_id=receipt_id,
        )
        for receipt_id, (correlation_id, mode, terminal) in state.items()
        if not terminal
    ]
    return polls[:limit]


def _command_bytes(
    topic: str,
    command: ModelTrajectoryEvaluationSubmit | ModelTrajectoryEvaluationPoll,
) -> bytes:
    return envelope_bytes_for(
        topic,
        command.model_dump(mode="json", exclude_none=True),
        command.correlation_id,
    )


async def _publish(
    messages: list[tuple[str, bytes, bytes]],
    *,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    omnibase_path: Path | None,
) -> None:
    open_bus = lab_run_bus_opener(bus, lane, kafka_bootstrap, omnibase_path)
    async with open_bus() as opened:
        for topic, key, value in messages:
            await opened.publish(topic, key, value)


def _emit(
    messages: list[tuple[str, bytes, bytes]],
    *,
    dry_run: bool,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    omnibase_path: Path | None,
) -> None:
    if dry_run:
        for _, _, value in messages:
            click.echo(value.decode("utf-8"))
        return
    if messages:
        try:
            asyncio.run(
                _publish(
                    messages,
                    bus=bus,
                    lane=lane,
                    kafka_bootstrap=kafka_bootstrap,
                    omnibase_path=omnibase_path,
                )
            )
        except LabRunBusError as exc:
            raise click.ClickException(str(exc)) from exc
    for topic, key, _ in messages:
        click.echo(f"published {key.decode('utf-8')} on {topic}")
    if not messages:
        click.echo("nothing to publish")


def _bus_options(function: Any) -> Any:
    for option in reversed(
        (
            click.option(
                "--bus",
                type=click.Choice(["kafka", "inmemory"]),
                default="kafka",
                show_default=True,
                help="kafka publishes to the lane's broker; inmemory is offline.",
            ),
            click.option("--lane", default=None, help="The declared lane of the bus."),
            click.option(
                "--kafka-bootstrap", default=None, help="The broker, stated directly."
            ),
            click.option(
                "--omnibase-path",
                type=click.Path(file_okay=False, path_type=Path),
                default=None,
                help="Where the lane declarations are read from.",
            ),
            click.option(
                "--dry-run",
                is_flag=True,
                help="Print each command's bytes, one per line; open no bus.",
            ),
        )
    ):
        function = option(function)
    return function


@click.group("trajectory-eval")
@click.pass_context
def trajectory_eval_group(ctx: click.Context) -> None:
    """Publish trajectory evaluation commands by hand."""
    ctx.ensure_object(dict)


@trajectory_eval_group.command("submit")
@click.option(
    "--run-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="An onex delegate run directory (result.txt, receipt.json, run.json).",
)
@click.option(
    "--from-json",
    "from_json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A submit command file, published byte for byte.",
)
@click.option(
    "--mode",
    type=click.Choice(["meta", "full"]),
    default=None,
    help="meta carries no prompt or response text; full carries both.",
)
@click.option(
    "--repo-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="The clone whose GitHub origin the run is attributed to.",
)
@_bus_options
def submit_command(
    run_dir: Path | None,
    from_json: Path | None,
    mode: str | None,
    repo_dir: Path,
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    omnibase_path: Path | None,
    dry_run: bool,
) -> None:
    """Publish one submit command."""
    topic = command_topics().submit
    if (run_dir is None) == (from_json is None):
        raise TrajectoryEvalUsageError("give exactly one of --run-dir and --from-json")
    if from_json is not None:
        if mode is not None:
            raise TrajectoryEvalUsageError("--mode applies to --run-dir only")
        raw = from_json.read_bytes()
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError("the file does not hold a JSON object")
            command = ModelTrajectoryEvaluationSubmit.model_validate(
                _payload_of(cast("dict[str, Any]", loaded))
            )
        except (ValueError, ValidationError) as exc:
            raise TrajectoryEvalUsageError(
                f"{from_json} is not a submit command: {exc}"
            ) from exc
        message = (topic, str(command.correlation_id).encode("utf-8"), raw)
    else:
        assert run_dir is not None
        if mode is None:
            raise TrajectoryEvalUsageError("--run-dir needs --mode meta or --mode full")
        command = submit_from_run_dir(
            run_dir, cast("Literal['meta', 'full']", mode), resolve_repository(repo_dir)
        )
        message = (
            topic,
            str(command.correlation_id).encode("utf-8"),
            _command_bytes(topic, command),
        )
    _emit(
        [message],
        dry_run=dry_run,
        bus=cast("BusKind", bus),
        lane=lane,
        kafka_bootstrap=kafka_bootstrap,
        omnibase_path=omnibase_path,
    )


@trajectory_eval_group.command("poll")
@click.option(
    "--outcomes",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Consumed outcomes, one JSON object per line, oldest first.",
)
@click.option(
    "--max",
    "limit",
    type=click.IntRange(min=1),
    default=DEFAULT_POLL_MAX,
    show_default=True,
    help="Publish at most this many poll commands.",
)
@_bus_options
def poll_command(
    outcomes: Path,
    limit: int,
    bus: str,
    lane: str | None,
    kafka_bootstrap: str | None,
    omnibase_path: Path | None,
    dry_run: bool,
) -> None:
    """Publish one poll command per accepted receipt still pending."""
    topic = command_topics().poll
    messages = [
        (topic, str(poll.correlation_id).encode("utf-8"), _command_bytes(topic, poll))
        for poll in pending_polls(outcomes, limit)
    ]
    _emit(
        messages,
        dry_run=dry_run,
        bus=cast("BusKind", bus),
        lane=lane,
        kafka_bootstrap=kafka_bootstrap,
        omnibase_path=omnibase_path,
    )
