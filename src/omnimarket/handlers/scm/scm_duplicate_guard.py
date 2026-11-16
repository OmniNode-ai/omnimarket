# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-duplicate-check`` and the check ``onex scm pr-create`` runs first (OMN-17427).

One fix gets one pull request. Before a lane opens a pull request, the guard folds the new branch against
the repository's open pull requests (PR watcher state plus ``git diff``) and the live ledger CLAIMs
(``node_pr_duplicate_guard_compute``). A duplicate is refused with exit 4, naming the pull request to
adopt; a source the guard cannot read exits 3 and opens nothing. There is no switch that turns it off.

A host with no fresh PR watcher state (a lab host) reads the open pull requests from the GitHub gateway instead
(``open_pr_facts``, OMN-22654); when that read fails too, the answer names both reads it lacks.

``--replaces OWNER/REPO#N`` (repeatable) names closed, unmerged pull requests of the same repository that the new
change replaces. The node reads each one's state itself; an overlap whose introducing commits all belong to such a
pull request is set aside (the answer's ``set_aside``), any other overlap still refuses, and a ``replaces`` entry that
is open, merged, of another repository or unreadable is refused with a typed reason (exit 5). With no ``--replaces``
the answer is what it always was.

Exit codes: 0 clear, 3 unchecked (a source could not be read), 4 a pull request or a live CLAIM already covers it,
5 a ``replaces`` entry is refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

import click

from omnimarket.lab_work.cli import _with_bus_options
from omnimarket.models.model_pr_duplicate_guard import (
    ModelPrDuplicateSnapshotRequest,
    validate_replaces,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    HandlerPrDuplicateSnapshot,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.ports.protocol_open_pr_facts_source import (
    ProtocolOpenPrFactsSource,
)

EXIT_CLEAR: Final[int] = 0
EXIT_UNCHECKED: Final[int] = 3
EXIT_DUPLICATE: Final[int] = 4
EXIT_REPLACES_REFUSED: Final[int] = 5
_REPO = "OWNER/REPO"


_EXIT_BY_STATE: Final[dict[str, int]] = {
    "clear": EXIT_CLEAR,
    "adopt": EXIT_DUPLICATE,
    "replaces_refused": EXIT_REPLACES_REFUSED,
}


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
    open_prs_source: ProtocolOpenPrFactsSource | None = None,
    replaces: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    """(exit code, the one JSON answer) for a pull request about to be opened."""
    snapshot = HandlerPrDuplicateSnapshot(open_prs_source).handle(
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
            replaces=replaces,
        )
    )
    if snapshot.request is None:
        return EXIT_UNCHECKED, {
            "state": "unchecked",
            "reason": snapshot.unchecked_reason,
        }
    result = HandlerPrDuplicateGuard().handle(snapshot.request)
    answer = result.model_dump(mode="json")
    return _EXIT_BY_STATE[result.state], answer


def refuse_or_continue(
    *,
    repo: str,
    head: str,
    base: str | None,
    title: str | None,
    body: str | None,
    lane: str | None,
    watcher_state: str | None,
    open_prs_source: ProtocolOpenPrFactsSource | None = None,
    replaces: tuple[str, ...] = (),
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
        open_prs_source=open_prs_source,
        replaces=replaces,
    )
    click.echo(json.dumps(answer, separators=(",", ":")))
    if code == EXIT_CLEAR:
        for note in answer.get("notes") or ():
            click.echo(f"PR-DUPLICATE-GUARD note: {note}", err=True)
        click.echo(f"PR-DUPLICATE-GUARD clear: {answer['message']}", err=True)
        return None
    if code == EXIT_UNCHECKED:
        click.echo(
            f"PR-DUPLICATE-GUARD unchecked, no pull request opened: {answer['reason']}",
            err=True,
        )
    elif code == EXIT_REPLACES_REFUSED:
        click.echo(
            f"PR-REPLACES-REFUSED, no pull request opened: {answer['message']}",
            err=True,
        )
    else:
        for item in answer.get("set_aside") or ():
            click.echo(
                f"PR-DUPLICATE-GUARD set aside {item['place']} of {item['pr']}: {item['why']}",
                err=True,
            )
        click.echo(
            f"PR-DUPLICATE-REFUSED, no pull request opened: {answer['message']}",
            err=True,
        )
    return code


def _replaces(raw: tuple[str, ...]) -> tuple[str, ...]:
    try:
        return validate_replaces(tuple(raw))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


@click.command("pr-duplicate-check")
@_with_bus_options
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
    "--replaces",
    multiple=True,
    metavar="OWNER/REPO#N",
    help="A closed, unmerged pull request of this repository that this change replaces (repeatable).",
)
@click.option(
    "--ledger",
    type=click.Path(dir_okay=False),
    default=None,
    help="The rolling ledger (default $ONEX_LEDGER_PATH).",
)
@click.option(
    "--timeout-s",
    type=float,
    default=60.0,
    show_default=True,
    help="How long the gateway read of the open pull requests may wait for its answer "
    "(read only when the host has no fresh PR watcher state).",
)
def duplicate_check_command(
    omnibase_path: Path | None,
    bus: str,
    bus_lane: str | None,
    kafka_bootstrap: str | None,
    timeout_s: float,
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
    replaces: tuple[str, ...],
    ledger: str | None,
) -> None:
    """Would this pull request duplicate an open one or a live CLAIM? Exit 0 clear, 4 duplicate, 3 unchecked."""
    if "/" not in target or "#" in target:
        raise click.UsageError(f"target {target!r} is not {_REPO}")
    if body is None and body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    # Imported here: scm_cli imports this module, and the gateway source opens the bus through scm_cli's opener.
    from omnimarket.handlers.scm import scm_cli

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
        replaces=_replaces(replaces),
        open_prs_source=scm_cli.gateway_open_prs_source(
            {
                "omnibase_path": omnibase_path,
                "bus": bus,
                "bus_lane": bus_lane,
                "kafka_bootstrap": kafka_bootstrap,
            },
            timeout_s,
        ),
    )
    click.echo(json.dumps(answer, separators=(",", ":")))
    if code == EXIT_CLEAR:
        click.echo(f"PR-DUPLICATE-GUARD clear: {answer['message']}", err=True)
    elif code == EXIT_DUPLICATE:
        click.echo(f"PR-DUPLICATE-REFUSED: {answer['message']}", err=True)
    elif code == EXIT_REPLACES_REFUSED:
        click.echo(f"PR-REPLACES-REFUSED: {answer['message']}", err=True)
    elif code == EXIT_UNCHECKED:
        click.echo(f"PR-DUPLICATE-GUARD unchecked: {answer['reason']}", err=True)
    raise SystemExit(code)
