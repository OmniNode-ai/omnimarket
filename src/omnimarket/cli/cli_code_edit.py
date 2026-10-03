# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex code-edit`` -- run the delegated code edit loop over a git worktree (OMN-20290).

    # Each model turn goes to the dev lane's deployed delegate orchestrator.
    onex code-edit run --worktree <path> --task-file task.md \\
        --writable 'src/pkg/*.py' --writable 'tests/test_pkg.py' \\
        --check 'tests=uv run pytest tests/test_pkg.py -q' \\
        --omnibase-path <workspace-root> --caller-lane <lane> --ticket OMN-1

    # Or one request file (ModelDelegatedCodeEditRequest JSON).
    onex code-edit run --request edit.json --omnibase-path <workspace-root>

    # Continue a delegate_failed loop with the same delegate flags.
    onex code-edit run --resume <correlation-id> --state-root .onex_state

    # Offline: each turn's delegate orchestrator runs in this process.
    onex code-edit run ... --bus inmemory --delegate-in-process

``run`` prints ONE line on stdout, the loop's compact result, and exits 0 when
the loop ends ``accepted`` (every declared check passed), 3 on any other loop
status, and 1 when the loop could not run at all. The loop receipt, with every
turn's ``onex delegate`` run id, the diff, the check results and the tool_use
rubric verdict, is ``<state root>/runs/<correlation id>/loop_receipt.json``.

Checks are argv, never shell strings: ``--check NAME=COMMAND`` is split with
shlex and run in the worktree with no shell. Run this on the host that holds
the worktree; model-written code executes there when a check runs.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import click

from omnimarket.delegated_code_edit.loop_ports import (
    IN_PROCESS_DELEGATE_FLAGS,
    DelegatedCodeEditPorts,
    deployed_lane_delegate_flags,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    LoopReceiptExistsError,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ResumeRefusedError,
)

EXIT_NOT_ACCEPTED = 3


def delegate_runner() -> Callable[[list[str]], subprocess.CompletedProcess[str]] | None:
    """How each ``onex delegate`` argv is run; ``None`` runs it as a process.
    A seam for tests."""
    return None


def read_file_list(path: Path | None) -> tuple[str, ...]:
    """The task's files from a ``--file-list`` file: one path per line, blank
    lines and ``#`` comments skipped, duplicates dropped, order kept."""
    if path is None:
        return ()
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unreadable file list {path}: {exc}") from exc
    names = (line.strip() for line in lines)
    return tuple(
        dict.fromkeys(name for name in names if name and not name.startswith("#"))
    )


def parse_check(spec: str) -> ModelDeclaredCheck:
    """``NAME=COMMAND`` as a declared check; its test targets come from the command."""
    name, sep, command = spec.partition("=")
    if not sep or not command.strip():
        raise click.BadParameter(f"{spec!r} is not NAME=COMMAND", param_hint="--check")
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        raise click.BadParameter(f"{spec!r}: {exc}", param_hint="--check") from exc
    found = [word for word in argv if word.startswith("tests/")]
    try:
        return ModelDeclaredCheck(name=name.strip(), argv=argv, targets=tuple(found))
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--check") from exc


def _resolve_onex(onex_path: Path | None, omnibase_path: Path | None) -> Path:
    if onex_path is not None:
        return onex_path
    if omnibase_path is not None:
        return omnibase_path / "omnibase_infra" / "scripts" / "onex"
    discovered = shutil.which("onex")
    if discovered is None:
        raise click.ClickException(
            "--onex was not given and the onex executable was not found on PATH"
        )
    return Path(discovered)


@click.group("code-edit")
def code_edit_group() -> None:  # stub-ok: a click group, subcommands added below
    """Run the delegated code edit loop over a git worktree."""


@code_edit_group.command("run")
@click.option(
    "--resume",
    "resume_id",
    type=click.UUID,
    default=None,
    help="Resume a delegate_failed loop from its receipt.",
)
@click.option(
    "--request",
    "request_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A ModelDelegatedCodeEditRequest JSON file, instead of the flags below.",
)
@click.option(
    "--worktree",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="The git worktree to edit.",
)
@click.option(
    "--task-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="The task text.",
)
@click.option(
    "--writable",
    multiple=True,
    help="A worktree-relative glob the model may write (repeatable, at least one).",
)
@click.option(
    "--context",
    "context_paths",
    multiple=True,
    help="A worktree-relative file shown in the first turn (repeatable).",
)
@click.option(
    "--file-list",
    "file_list_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A text file naming the task's files, one worktree-relative path per "
    "line (blank lines and lines starting with # are skipped). replace_in_files "
    "then reaches only these files; a glob narrows the list, never widens it.",
)
@click.option(
    "--check",
    "check_specs",
    multiple=True,
    help="NAME=COMMAND, a declared check (repeatable, at least one).",
)
@click.option(
    "--formatter",
    default=None,
    help="COMMAND of the formatter the format tool runs, split with shlex; the "
    "file path is appended (e.g. 'uv run ruff format'). Omitted: no format tool.",
)
@click.option("--max-turns", type=click.IntRange(1, 40), default=20, show_default=True)
@click.option(
    "--caller-lane",
    default=None,
    help="The ledger lane each delegate turn is attributed to. Omitted, onex "
    "delegate resolves it from the lane environment as usual.",
)
@click.option("--ticket", default=None, help="OMN-<n> stamped on each turn.")
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
    "--omnibase-path",
    envvar="OMNIBASE_PATH",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace root holding the onex wrapper. Bound to $OMNIBASE_PATH.",
)
@click.option(
    "--onex",
    "onex_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The onex executable each turn runs. Default: <workspace root>/"
    "omnibase_infra/scripts/onex when --omnibase-path is given, otherwise the "
    "onex on PATH.",
)
@click.option(
    "--delegate-lane",
    default="dev",
    show_default=True,
    help="The lane whose deployed orchestrator answers the delegate turns.",
)
@click.option(
    "--delegate-in-process",
    is_flag=True,
    help="Run each turn's delegate orchestrator in this process on the in-memory bus.",
)
@click.option(
    "--max-tokens",
    type=click.IntRange(min=1),
    default=None,
    help="Per-turn response budget; omitted, the routing contract decides.",
)
def run_command(
    resume_id: uuid.UUID | None,
    request_path: Path | None,
    worktree: Path | None,
    task_file: Path | None,
    writable: tuple[str, ...],
    context_paths: tuple[str, ...],
    file_list_path: Path | None,
    check_specs: tuple[str, ...],
    formatter: str | None,
    max_turns: int,
    caller_lane: str | None,
    ticket: str | None,
    new_correlation: bool,
    state_root: Path,
    omnibase_path: Path | None,
    onex_path: Path | None,
    delegate_lane: str,
    delegate_in_process: bool,
    max_tokens: int | None,
) -> None:
    """Run one delegated code edit loop and print its compact result."""
    if resume_id is not None:
        conflicts = {
            "--request": request_path is not None,
            "--worktree": worktree is not None,
            "--task-file": task_file is not None,
            "--writable": bool(writable),
            "--context": bool(context_paths),
            "--file-list": file_list_path is not None,
            "--check": bool(check_specs),
            "--formatter": formatter is not None,
            "--new-correlation": new_correlation,
        }
        for flag, present in conflicts.items():
            if present:
                raise click.ClickException(f"--resume conflicts with {flag}")
        try:
            receipt = json.loads(
                (state_root / "runs" / str(resume_id) / "loop_receipt.json").read_text()
            )
            if not isinstance(receipt, dict):
                raise ValueError("receipt is not an object")
            request = ModelDelegatedCodeEditRequest.model_validate(receipt["request"])
        except (OSError, ValueError, KeyError, UnicodeError, RecursionError) as exc:
            raise click.ClickException(f"unreadable receipt: {exc}") from exc
    elif request_path is not None:
        try:
            request = ModelDelegatedCodeEditRequest.model_validate_json(
                request_path.read_text()
            )
        except ValueError as exc:
            raise click.ClickException(f"unreadable request: {exc}") from exc
        if new_correlation:
            request = request.model_copy(update={"correlation_id": str(uuid.uuid4())})
    else:
        if worktree is None or task_file is None or not writable or not check_specs:
            raise click.ClickException(
                "without --request, give --worktree, --task-file, at least one "
                "--writable and at least one --check"
            )
        try:
            request = ModelDelegatedCodeEditRequest(
                correlation_id=str(uuid.uuid4()),
                file_list=read_file_list(file_list_path),
                task=task_file.read_text(),
                workspace_root=str(worktree.resolve()),
                writable_globs=writable,
                context_paths=context_paths,
                checks=tuple(parse_check(spec) for spec in check_specs),
                formatter=tuple(shlex.split(formatter)) if formatter else (),
                max_turns=max_turns,
                caller_lane=caller_lane,
                ticket=ticket,
            )
        except ValueError as exc:
            raise click.ClickException(f"invalid request: {exc}") from exc
    ports = DelegatedCodeEditPorts(
        onex=_resolve_onex(onex_path, omnibase_path),
        state_root=state_root.resolve(),
        delegate_flags=(
            IN_PROCESS_DELEGATE_FLAGS
            if delegate_in_process
            else deployed_lane_delegate_flags(delegate_lane)
        ),
        run_delegate=delegate_runner(),
        max_tokens=max_tokens,
    )
    try:
        result = HandlerDelegatedCodeEditOrchestrator(ports).run(
            request, resume=resume_id is not None
        )
    except (LoopReceiptExistsError, ResumeRefusedError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result.model_dump(mode="json"), separators=(",", ":")))
    if result.status != EnumCodeEditStatus.ACCEPTED:
        sys.exit(EXIT_NOT_ACCEPTED)


__all__ = ["code_edit_group", "parse_check", "read_file_list"]
