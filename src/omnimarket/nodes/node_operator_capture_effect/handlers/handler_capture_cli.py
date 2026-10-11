# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_operator_capture_effect <command>`` (OMN-20905, OMN-20906).

The bus side runs inside the work-ledger serve process on the ledger host, started with
``--operator-capture`` (RULING 2026-10-10T22:40:43Z: async off the bus, no capture hooks); it
takes every operator prompt from the content-capture topic and records its decisions, asks,
ideas and preferences on the ledger. These commands are the operator's hand tools beside it:

* ``process``: one worker run over the store's inbox (what the bus host retries on its own).
* ``digest``: print the open-ask digest; ``--push-overdue`` also sends the attention push.

Environment: ``ONEX_LEDGER_PATH`` (the ledger), ``ONEX_OPERATOR_CAPTURE_LEDGER_BIN`` (its
append command), ``ONEX_OPERATOR_CAPTURE_ONEX_BIN`` (the onex CLI for delegation, else ``onex``
on PATH), ``ONEX_OPERATOR_CAPTURE_DIR`` (the store), ``ONEX_ALERT_CHANNEL_LIB`` and
``ONEX_ALERT_CHANNEL_ENV_FILE`` (the push channel).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from omnimarket.models.operator_capture import (
    ModelCaptureDigestRequest,
    ModelCaptureDigestResult,
    ModelCaptureProcessRequest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    onex_delegate_runner,
    onex_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
    HandlerCaptureDigest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
    HandlerCaptureProcess,
)

ALERT_LIB_ENV = "ONEX_ALERT_CHANNEL_LIB"
ALERT_ENV_FILE_ENV = "ONEX_ALERT_CHANNEL_ENV_FILE"
LEDGER_PATH_ENV = "ONEX_LEDGER_PATH"
_ALERT_COMMAND = (
    'set -a; [ -n "$1" ] && [ -r "$1" ] && . "$1" >/dev/null 2>&1; set +a; '
    '. "$2" && alert_channel_send "$3" "$4"'
)


def _ledger_path() -> Path:
    value = os.environ.get(LEDGER_PATH_ENV, "").strip()
    if not value:
        raise click.UsageError(f"{LEDGER_PATH_ENV} is unset: no ledger to record on")
    return Path(value)


def alert_channel_push(text: str) -> tuple[bool, str]:
    lib = os.environ.get(ALERT_LIB_ENV, "").strip()
    if not lib:
        return False, f"{ALERT_LIB_ENV} is unset: no push channel"
    env_file = os.environ.get(ALERT_ENV_FILE_ENV, "").strip()
    try:
        done = subprocess.run(
            ["bash", "-c", _ALERT_COMMAND, "_", env_file, lib, "operator-asks", text],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"push did not run: {exc}"
    return done.returncode == 0, f"alert channel exit {done.returncode}"


def _echo_json(data: dict[str, Any]) -> None:
    click.echo(json.dumps(data, default=str))


@click.group("operator-capture")
@click.pass_context
def operator_capture_group(ctx: click.Context) -> None:
    """Record what the operator decides and asks, from the bus, on the ledger."""
    ctx.ensure_object(dict)


@operator_capture_group.command("process")
@click.option("--max", "max_captures", type=int, default=20, show_default=True)
@click.option(
    "--delegate-lane",
    default=None,
    help="The bus lane for delegated classification; unset: the deterministic fallback.",
)
def process_command(max_captures: int, delegate_lane: str | None) -> None:
    """One worker run over the store's inbox."""
    result = HandlerCaptureProcess(
        onex_delegate_runner(delegate_lane), onex_ledger_appender()
    ).handle(
        ModelCaptureProcessRequest(
            store_dir=capture_store.store_dir(),
            ledger_path=_ledger_path(),
            max_captures=max_captures,
            delegate=delegate_lane is not None,
        )
    )
    _echo_json({"at": datetime.now(UTC).isoformat(), **result.model_dump(mode="json")})
    if result.errors and not result.processed:
        sys.exit(1)


def digest(push_overdue: bool) -> ModelCaptureDigestResult:
    return HandlerCaptureDigest(push=alert_channel_push).handle(
        ModelCaptureDigestRequest(
            store_dir=capture_store.store_dir(),
            ledger_path=_ledger_path(),
            now=datetime.now(UTC),
            push_overdue=push_overdue,
        )
    )


@operator_capture_group.command("digest")
@click.option("--push-overdue", is_flag=True, help="Also push asks waiting over a day.")
def digest_command(push_overdue: bool) -> None:
    """Print the open-ask digest."""
    click.echo(digest(push_overdue).digest)


def main() -> None:
    operator_capture_group()


__all__ = ["alert_channel_push", "digest", "main", "operator_capture_group"]
