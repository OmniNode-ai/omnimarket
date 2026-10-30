# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab host table judge command used by the lab-work CLI and launchers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence

import click
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from .handlers import HandlerLabPlacement, HostTableError, load_host_table
from .models import LAB_WORK_WEIGHT_ENV, ModelLabPlacementRequest

EXIT_NO_ADVERTISEMENT = 69


def unit_from_words(words: Sequence[str]) -> tuple[str | None, str | None]:
    """(repository, work kind) a lab-run argument list names: ``--repo`` (a bare name or owner/name), else the origin of
    ``--clone`` or of the working directory; ``--kind``. None for what the arguments do not say."""
    flags: dict[str, str] = {}
    held: str | None = None
    for word in words:
        if held is not None:
            flags[held], held = word, None
        elif word in ("--repo", "--clone", "--kind"):
            held = word
        elif word.startswith(("--repo=", "--clone=", "--kind=")):
            flags[word.split("=", 1)[0]] = word.split("=", 1)[1]
    repo = flags.get("--repo")
    if repo is None:
        done = subprocess.run(
            ["git", "-C", flags.get("--clone", "."), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(dict(os.environ)),
        )
        repo = (done.stdout.strip() or None) if done.returncode == 0 else None
    return repo, flags.get("--kind")


@click.command("judge")
@click.option(
    "--words",
    default=None,
    help="JSON list of the arguments the lab-run launcher was given before `--`: the unit's "
    "--repo (else the origin of --clone or of the working directory) and --kind are read from it.",
)
@click.option("--repo", default=None, help="owner/name or the bare repository name.")
@click.option(
    "--kind",
    type=click.Choice(["test", "build", "lint", "code_draft", "other"]),
    default=None,
)
@click.option("--weight", default=None, envvar=LAB_WORK_WEIGHT_ENV)
@click.argument("host_names", nargs=-1, required=True)
def judge_command(
    words: str | None,
    repo: str | None,
    kind: str | None,
    weight: str | None,
    host_names: tuple[str, ...],
) -> None:
    """Print the host table's verdict on each named host for one unit, as JSON.

    This is the one rule for what a host may take (``available``, work kind, weight, capabilities
    such as AVX2, the host's own memory bar and speed); the bus placement applies it itself and
    the plugin's lab-run launcher applies it to the hosts it ranks. Exits 69 when the unit, the
    host table or the placement rules cannot be read, never a guess."""
    try:
        said_repo, said_kind = unit_from_words(json.loads(words) if words else [])
        if (repo or said_repo) is None:
            raise HostTableError(
                "the unit's repository is not named (pass --repo, or run in a git checkout)"
            )
        document = load_host_table()
        judged = HandlerLabPlacement().handle(
            ModelLabPlacementRequest(
                repo=repo or said_repo or "",
                kind=kind or said_kind or "other",
                weight=weight,
                host_names=host_names,
                table=document.hosts,
                unit_needs=document.unit_needs,
            )
        )
    except (HostTableError, ValueError) as exc:
        click.echo(f"lab-work: {exc}", err=True)
        sys.exit(EXIT_NO_ADVERTISEMENT)
    click.echo(
        json.dumps(
            {
                "unit": judged.unit.model_dump(mode="json"),
                "verdicts": [v.model_dump(mode="json") for v in judged.verdicts],
            },
            indent=2,
        )
    )
