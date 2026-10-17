# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex lab claude-token``: the operator's shim over the lab Claude token effect (OMN-19998).

    claude setup-token                                   # the operator's act: a browser sign-in
    onex lab claude-token set --host h201 --account <a>  # paste the token at the hidden prompt, or pipe it
    onex lab claude-token add-spare --account <a>        # keep a spare account in the pool
    onex lab claude-token rotate-limited [--host h201]   # move benched hosts onto a spare

The token is read from stdin only (a hidden prompt on a terminal), never from an
argument, and never echoed. Host ssh targets come from the lab host table placement
reads; the ``.limited-<host>`` markers from placement's directory.
"""

from __future__ import annotations

import os
import sys
from getpass import getpass
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_lab_claude_token import (
    LIMITED_PREFIX,
    HandlerLabClaudeToken,
)
from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabClaudeTokenRequest,
    ModelLabTokenHost,
)
from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_result import (
    ModelLabClaudeTokenResult,
)

# The lab host table is a deployment fact kept outside this repository, named by
# ONEX_LAB_RUN_HOSTS, the variable placement, ``onex delegate`` and ``onex secret`` read.
LAB_HOSTS_ENV = "ONEX_LAB_RUN_HOSTS"


def lab_table_path() -> Path | None:
    """The lab host table this machine reads, or None off the lab."""
    configured = os.environ.get(LAB_HOSTS_ENV, "").strip()
    return Path(configured) if configured else None


def placement_dir() -> Path:
    """Placement's directory of ``.limited-<host>`` markers."""
    if os.environ.get("LANDING_PLACEMENT_DIR"):
        return Path(os.environ["LANDING_PLACEMENT_DIR"])
    return Path.home() / ".local" / "state" / "omni" / "landing" / "placement"


def lab_hosts(path: Path | None = None) -> dict[str, ModelLabTokenHost]:
    """The lab host table's rows by name; a row marked ``available: false`` is still a host."""
    path = path or lab_table_path()
    if path is None or not path.is_file():
        raise click.ClickException(
            f"no lab host table at {path}; set {LAB_HOSTS_ENV} to the lab host table"
        )
    rows: Any = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "hosts"
    ) or []
    out: dict[str, ModelLabTokenHost] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("name"):
            out[str(row["name"])] = ModelLabTokenHost(
                name=str(row["name"]),
                target=row.get("target"),
                local=bool(row.get("local", False)),
            )
    return out


def build_handler() -> HandlerLabClaudeToken:
    from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_lab_host_shell import (
        HandlerLabHostShell,
    )
    from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_local_token_store import (
        HandlerLocalTokenStore,
    )

    return HandlerLabClaudeToken(
        store=HandlerLocalTokenStore(), shell=HandlerLabHostShell()
    )


def _read_token(label: str, *, required: bool) -> SecretStr | None:
    """The token from stdin, or a hidden prompt on a terminal. Never echoed."""
    if sys.stdin is not None and sys.stdin.isatty():
        if not required:
            return None
        value = getpass(f"Paste the Claude token for {label} (input hidden): ")
    else:
        value = sys.stdin.read() if sys.stdin is not None else ""
    value = value.strip()
    return SecretStr(value) if value else None


def _host(name: str) -> ModelLabTokenHost:
    hosts = lab_hosts()
    if name not in hosts:
        raise click.ClickException(
            f"{name} is not in the lab host table ({', '.join(sorted(hosts))})"
        )
    return hosts[name]


def _run(request_fields: dict[str, Any]) -> ModelLabClaudeTokenResult:
    try:
        request = ModelLabClaudeTokenRequest(**request_fields)
    except ValidationError as exc:
        raise click.ClickException(
            "; ".join(str(e["msg"]) for e in exc.errors())
        ) from None
    return build_handler().handle(request)


def _emit(result: ModelLabClaudeTokenResult) -> None:
    for line in result.lines:
        click.echo(line)
    for event in result.events:
        click.echo(f"event: {event.model_dump_json()}")


lab_group = click.Group("lab", help="Lab host operations.")
claude_token_group = click.Group(
    "claude-token",
    help="Store, deploy, probe and rotate a lab host's Claude token. Values are never printed.",
)
lab_group.add_command(claude_token_group)


@claude_token_group.command("set")
@click.option(
    "--host", "host_name", required=True, help="Lab host name from the lab host table."
)
@click.option(
    "--account",
    required=True,
    help="The Claude account the token belongs to (a short slug).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan by reference; store, deploy and probe nothing.",
)
def set_command(host_name: str, account: str, dry_run: bool) -> None:
    """Store a token for HOST, deploy it mode 0600, probe it, and clear the host's limit marker.

    A token that does not answer is taken back off the host and the previous one restored."""
    token = _read_token(f"{host_name}/{account}", required=not dry_run)
    result = _run(
        {
            "operation": "set",
            "host": _host(host_name),
            "account": account,
            "token": token,
            "placement_dir": str(placement_dir()),
            "dry_run": dry_run,
        }
    )
    _emit(result)
    if result.outcome == "restored":
        sys.exit(1)


@claude_token_group.command("add-spare")
@click.option(
    "--account", required=True, help="The spare Claude account (a short slug)."
)
@click.option("--dry-run", is_flag=True)
def add_spare_command(account: str, dry_run: bool) -> None:
    """Keep a token as a spare account the rotation can move a limited host onto."""
    token = _read_token(f"spare {account}", required=not dry_run)
    _emit(
        _run(
            {
                "operation": "add_spare",
                "account": account,
                "token": token,
                "placement_dir": str(placement_dir()),
                "dry_run": dry_run,
            }
        )
    )


@claude_token_group.command("rotate-limited")
@click.option(
    "--host",
    "host_name",
    default=None,
    help="One host; default every host with a .limited marker.",
)
@click.option("--dry-run", is_flag=True)
def rotate_limited_command(host_name: str | None, dry_run: bool) -> None:
    """Move each host placement benched (``.limited-<host>``) onto a spare account with headroom."""
    directory = placement_dir()
    if host_name:
        names = [host_name]
    else:
        names = (
            sorted(
                p.name[len(LIMITED_PREFIX) :]
                for p in directory.glob(f"{LIMITED_PREFIX}*")
                if not p.name.endswith(".tmp")
            )
            if directory.is_dir()
            else []
        )
    if not names:
        click.echo(f"no host carries a {LIMITED_PREFIX}<host> marker in {directory}")
        return
    for name in names:
        _emit(
            _run(
                {
                    "operation": "rotate_limited",
                    "host": _host(name),
                    "placement_dir": str(directory),
                    "dry_run": dry_run,
                }
            )
        )
