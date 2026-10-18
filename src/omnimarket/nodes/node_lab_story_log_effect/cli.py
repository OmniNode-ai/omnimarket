# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex lab-story-log``: write one ISO week of the work ledger as a sanitized story log.

    onex lab-story-log --week 2026-W42
    onex lab-story-log --week previous --publish      # what the Sunday host tick runs

Every path comes from an option or the environment the host tick carries: the ledger from
``ONEX_LEDGER_PATH`` (archive files whose split date reaches into the week are read too), the
hygiene vocabulary from ``OMNIBASE_INTERNAL_HOME``, the checkout the log is written into from
``ONEX_STORY_LOG_CHECKOUT``, and for ``--publish`` the pull request's repository from
``ONEX_STORY_LOG_REPOSITORY`` and the worktree directory from ``ONEX_STORY_LOG_WORKTREES``.
Nothing is guessed: an unset variable the run needs is a usage error naming it (exit 2). A
private name that survives replacement exits 1 and writes nothing.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from omnimarket.nodes.node_lab_story_log_effect.handlers.handler_lab_story_log import (
    HandlerLabStoryLog,
    resolve_week,
)
from omnimarket.nodes.node_lab_story_log_effect.handlers.hygiene import (
    HygieneVocabularyError,
    LabStoryLogLeakError,
)
from omnimarket.nodes.node_lab_story_log_effect.models import ModelLabStoryLogRequest
from omnimarket.nodes.node_lab_story_log_effect.publish import (
    PublishError,
    publish_story_log,
)

_SPLIT = re.compile(r"(\d{4}-\d{2}-\d{2})-split\.md$")


def ledger_paths(ledger: Path, week_start: datetime) -> tuple[Path, ...]:
    """The live ledger plus every archive split dated on or after the week's first day."""
    paths = [ledger]
    archive = ledger.parent / "archive"
    if archive.is_dir():
        for candidate in sorted(archive.glob("*-split.md")):
            found = _SPLIT.search(candidate.name)
            if found and found.group(1) >= week_start.date().isoformat():
                paths.append(candidate)
    return tuple(paths)


@click.command("lab-story-log")
@click.option(
    "--week",
    default="previous",
    show_default=True,
    help="ISO week such as 2026-W42, or 'previous'.",
)
@click.option(
    "--ledger",
    type=click.Path(path_type=Path),
    envvar="ONEX_LEDGER_PATH",
    required=True,
    help="The live work ledger.",
)
@click.option(
    "--vocabulary-home",
    type=click.Path(path_type=Path),
    envvar="OMNIBASE_INTERNAL_HOME",
    required=True,
    help="Checkout holding the hygiene vocabulary files.",
)
@click.option(
    "--checkout",
    type=click.Path(path_type=Path),
    envvar="ONEX_STORY_LOG_CHECKOUT",
    required=True,
    help="Canonical clone the log is written into (or published from).",
)
@click.option(
    "--repository",
    envvar="ONEX_STORY_LOG_REPOSITORY",
    default=None,
    help="OWNER/REPO the pull request is opened on; required with --publish.",
)
@click.option(
    "--worktree-root",
    type=click.Path(path_type=Path),
    envvar="ONEX_STORY_LOG_WORKTREES",
    default=None,
    help="Directory the publish worktree is cut under; required with --publish.",
)
@click.option("--dry-run", is_flag=True, help="Render and check; write nothing.")
@click.option(
    "--publish",
    is_flag=True,
    help="Commit the log on a branch of the checkout and open the pull request.",
)
def lab_story_log_command(
    week: str,
    ledger: Path,
    vocabulary_home: Path,
    checkout: Path,
    repository: str | None,
    worktree_root: Path | None,
    dry_run: bool,
    publish: bool,
) -> None:
    """Write one ISO week of the work ledger as a sanitized lab story log."""
    if dry_run and publish:
        raise click.UsageError("--dry-run and --publish cannot be combined")
    if publish and (repository is None or worktree_root is None):
        raise click.UsageError(
            "--publish needs --repository (ONEX_STORY_LOG_REPOSITORY) and "
            "--worktree-root (ONEX_STORY_LOG_WORKTREES)"
        )
    try:
        label, start, _ = resolve_week(week, datetime.now(UTC))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    home = vocabulary_home
    root = checkout
    output_root = root
    if publish and worktree_root is not None:
        output_root = worktree_root / f"lab-story-log-{label.lower()}" / "render"
    request = ModelLabStoryLogRequest(
        week=label,
        ledger_paths=ledger_paths(ledger, start),
        hygiene_vocabulary_path=home
        / "docs/workflows/_shared/public_repo_hygiene_vocabulary.yaml",
        person_names_path=home / "vocabularies/person_names.txt",
        lab_vocabulary_path=home
        / "vocabularies/public_repo_hygiene_lab_vocabulary.yaml",
        output_root=output_root,
        dry_run=dry_run,
    )
    try:
        result = HandlerLabStoryLog().handle(request)
    except (HygieneVocabularyError, LabStoryLogLeakError, OSError) as exc:
        click.echo(f"lab-story-log: refused: {exc}", err=True)
        sys.exit(1)
    summary = {
        k: v for k, v in result.model_dump(mode="json").items() if k != "markdown"
    }
    if publish:
        try:
            outcome = publish_story_log(
                clone=root,
                repository=repository or "",
                worktree_root=worktree_root or Path(),
                label=label,
                markdown=result.markdown,
                summary=(
                    f"Weekly lab story log for ISO week {label}: {result.rows_in_week} ledger rows, "
                    f"{result.rulings} rulings, {result.friction_rows} friction rows and "
                    f"{result.merged_prs} merged pull requests, private names replaced by roles."
                ),
            )
        except PublishError as exc:
            click.echo(f"lab-story-log: publish failed: {exc}", err=True)
            sys.exit(1)
        summary["publish"] = {
            "published": outcome.published,
            "branch": outcome.branch,
            "detail": outcome.detail,
        }
    click.echo(json.dumps(summary, indent=2, sort_keys=True))


__all__ = ["lab_story_log_command", "ledger_paths"]
