# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ledger row sent over the bus append path, never written to the ledger file.

The work-ledger append effect node answers ``onex work-ledger append``: it takes the exact row on
standard input, publishes it to the ledger host over the lane's declared bus and prints one receipt.
A caller that has no ledger file to name runs it through this module and gets ``(ok, detail)``.
A row whose first attempt got no answer is sent again under the same request id, so the ledger host
answers a duplicate and the row lands once.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import uuid
from collections.abc import Mapping

DEFAULT_COMMAND = "onex work-ledger"
# A unit's declared bus append command, already ending in ``append`` with the bus lane and workspace root bound
# (OMN-24562); the default command binds neither, so a unit with no workspace root exits 69 on every append.
HOST_BUS_APPEND_COMMAND_ENV = "ONEX_LEDGER_BUS_APPEND_COMMAND"
DEFAULT_TIMEOUT_S = 360


def request_id_for(namespace: uuid.UUID, key: str) -> uuid.UUID:
    """A stable request id for ``key``: the same key is the same append."""
    return uuid.uuid5(namespace, key)


def append_row_via_bus(
    row: str,
    *,
    lane: str,
    request_id: uuid.UUID,
    command_env: str,
    environ: Mapping[str, str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[bool, str]:
    """Send ``row`` to the ledger host as ``lane``; ``command_env`` may name a different command."""
    values = os.environ if environ is None else environ
    if command_env in values:
        command = [*shlex.split(values[command_env]), "append"]
        if len(command) == 1:
            return False, "ledger command is empty"
    elif values.get(HOST_BUS_APPEND_COMMAND_ENV, "").strip():
        command = shlex.split(values[HOST_BUS_APPEND_COMMAND_ENV])
    else:
        command = [*shlex.split(DEFAULT_COMMAND), "append"]
    try:
        done = subprocess.run(
            [
                *command,
                "--lane",
                lane,
                "--host",
                socket.gethostname().split(".", 1)[0],
                "--request-id",
                str(request_id),
            ],
            input=row,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ledger append did not run: {exc}"
    return (
        done.returncode == 0,
        f"ledger append exit {done.returncode}: {done.stderr[-200:]}",
    )
