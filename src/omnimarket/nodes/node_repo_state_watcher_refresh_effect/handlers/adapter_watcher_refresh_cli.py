# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex repo-state-refresh``: the declared command that refreshes a host's PR watcher state (OMN-23033).

A host's repo-state overlay names this command in its watcher refresh environment variable
(``onex repo-state-refresh --repo {repo}``); the repository state query runs it when a pull
request, checks or job read finds the watcher state older than the bus's last event. Exit 0
only when the state's read time moved forward.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.handlers.adapter_watcher_refresh import (
    build_handler,
    load_overlay,
)
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.models.model_watcher_refresh import (
    OVERLAY_FILE_ENV,
    ModelWatcherRefreshRequest,
)

EXIT_CONFIG = 2
EXIT_NOT_REFRESHED = 1


@click.command("repo-state-refresh")
@click.option("--repo", required=True, help="owner/name")
@click.option(
    "--overlay",
    "overlay_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=f"Overlay file; else {OVERLAY_FILE_ENV}, else the overlay roots.",
)
def watcher_refresh_command(repo: str, overlay_path: Path | None) -> None:
    """Refresh this host's PR watcher state; print the result; exit 0 when it moved forward."""
    try:
        request = ModelWatcherRefreshRequest(repo=repo)
        handler = build_handler(load_overlay(overlay_path))
    except (NodeOverlayError, ValidationError, ValueError) as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    result = handler.handle(request)
    click.echo(result.model_dump_json())
    sys.exit(0 if result.ok else EXIT_NOT_REFRESHED)


def main() -> None:
    watcher_refresh_command()


if __name__ == "__main__":
    main()


__all__ = ["main", "watcher_refresh_command"]
