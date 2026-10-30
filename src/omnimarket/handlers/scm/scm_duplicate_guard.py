# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-duplicate-check`` and the check ``onex scm pr-create`` runs first (OMN-17427).

One fix gets one pull request. Before a lane opens a pull request, the guard folds the new branch against
the repository's open pull requests (PR watcher state plus ``git diff``) and the live ledger CLAIMs
(``node_pr_duplicate_guard_compute``). A duplicate is refused with exit 4, naming the pull request to
adopt; a source the guard cannot read exits 3 and opens nothing. There is no switch that turns it off.

Exit codes: 0 clear, 3 unchecked (a source could not be read), 4 a pull request or a live CLAIM already covers it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

import click

from omnimarket.models.model_pr_duplicate_guard import ModelPrDuplicateSnapshotRequest
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    HandlerPrDuplicateSnapshot,
)

EXIT_CLEAR: Final[int] = 0
EXIT_UNCHECKED: Final[int] = 3
EXIT_DUPLICATE: Final[int] = 4
_REPO = "OWNER/REPO"


def check(
    *,
    repo: str,
    head: str,
    base: str | None,
    title: str | None,
    body: str | None,
    lane: str | None,
    cause: str | None = None,
    watcher_state: str | None = None,
    ledger: str | None = None,
    clone: str | None = None,
    worktree: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """(exit code, the one JSON answer) for a pull request about to be opened."""
    snapshot = HandlerPrDuplicateSnapshot().handle(
        ModelPrDuplicateSnapshotRequest(
            repo=repo,
            head_ref=head.rsplit(":", 1)[-1],
            base=base,
            title=title or "",
            body=body or "",
            lane=lane,
            cause=cause,
            watcher_state=watcher_state,
            ledger=ledger,
            clone=clone,
            worktree=worktree,
        )
    )
    if snapshot.request is None:
        return EXIT_UNCHECKED, {
            "state": "unchecked",
            "reason": snapshot.unchecked_reason,
        }
    result = HandlerPrDuplicateGuard().handle(snapshot.request)
    answer = result.model_dump(mode="json")
    return (EXIT_CLEAR if result.state == "clear" else EXIT_DUPLICATE), answer


def refuse_or_continue(
    *,
    repo: str,
    head: str,
    base: str | None,
    title: str | None,
    body: str | None,
    lane: str | None,
    watcher_state: str | None,
) -> int | None:
    """``onex scm pr-create``'s step: None to go on and create, else the exit code, with the refusal printed."""
    code, answer = check(
        repo=repo,
        head=head,
        base=base,
        title=title,
        body=body,
        lane=lane,
        watcher_state=watcher_state,
    )
    click.echo(json.dumps(answer, separators=(",", ":")))
    if code == EXIT_CLEAR:
        for note in answer.get("notes") or ():
            click.echo(f"PR-DUPLICATE-GUARD note: {note}", err=True)
        return None
    if code == EXIT_UNCHECKED:
        click.echo(
            f"PR-DUPLICATE-GUARD unchecked, no pull request opened: {answer['reason']}",
            err=True,
        )
    else:
        click.echo(
            f"PR-DUPLICATE-REFUSED, no pull request opened: {answer['message']}",
            err=True,
        )
    return code


@click.command("pr-duplicate-check")
@click.argument("target")
@click.option(
    "--head", required=True, help="The branch the pull request would be opened from."
)
@click.option(
    "--base",
    default=None,
    help="The base branch (default: the clone's default branch).",
)
@click.option("--title", default=None)
@click.option("--body", default=None)
@click.option("--body-file", type=click.Path(dir_okay=False), default=None)
@click.option(
    "--lane",
    envvar="ONEX_LANE",
    default=None,
    help="The asking lane ($ONEX_LANE): its own CLAIMs are not a duplicate, and its cause= cell says what it fixes.",
)
@click.option(
    "--cause",
    default=None,
    help="<repo>:<check> the change fixes, when the lane has no CLAIM cell yet.",
)
@click.option(
    "--worktree",
    type=click.Path(file_okay=False),
    default=None,
    help="Where the branch is checked out (default: the working directory).",
)
@click.option(
    "--clone",
    type=click.Path(file_okay=False),
    default=None,
    help="The repository's canonical clone.",
)
@click.option("--watcher-state", type=click.Path(dir_okay=False), default=None)
@click.option(
    "--ledger",
    type=click.Path(dir_okay=False),
    default=None,
    help="The rolling ledger (default $ONEX_LEDGER_PATH).",
)
def duplicate_check_command(
    target: str,
    head: str,
    base: str | None,
    title: str | None,
    body: str | None,
    body_file: str | None,
    lane: str | None,
    cause: str | None,
    worktree: str | None,
    clone: str | None,
    watcher_state: str | None,
    ledger: str | None,
) -> None:
    """Would this pull request duplicate an open one or a live CLAIM? Exit 0 clear, 4 duplicate, 3 unchecked."""
    if "/" not in target or "#" in target:
        raise click.UsageError(f"target {target!r} is not {_REPO}")
    if body is None and body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    code, answer = check(
        repo=target,
        head=head,
        base=base,
        title=title,
        body=body,
        lane=lane,
        cause=cause,
        watcher_state=watcher_state,
        ledger=ledger or os.environ.get("ONEX_LEDGER_PATH"),
        clone=clone,
        worktree=worktree,
    )
    click.echo(json.dumps(answer, separators=(",", ":")))
    if code == EXIT_DUPLICATE:
        click.echo(f"PR-DUPLICATE-REFUSED: {answer['message']}", err=True)
    elif code == EXIT_UNCHECKED:
        click.echo(f"PR-DUPLICATE-GUARD unchecked: {answer['reason']}", err=True)
    raise SystemExit(code)
