# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_rows_loader_settings_effect``: this host's rows loader settings, or the loader run with them.

Resolves the loader command of the host it runs on from the ``ledger_rows_loader_hosts`` overlay and the
loader overlay's fallback flag (overlays found under ``ONEX_SKILL_OVERLAY_ROOTS``), prints one JSON line on
stdout and, when no row was found or an overlay was refused, a ``LOADER-SETTINGS`` line on stderr that names
the reason. The caller names its registry root with ``--registry-root``. Exit 0: the host's row was found. Exit
10: no row for this host (the JSON still carries the overlay's fallback flag). Exit 69: an overlay is missing or
malformed; the stderr line names it.

With ``--run`` the node runs the host's loader with the arguments after it (``--reader``, ``--window-hours``,
``--file-ledger``, ``--archive-glob``, passed through) and the exit code says what the reader does: 0 the loader's rows
are on stdout; 10 the loader read the file itself (counted by the loader), rows on stdout; 11 no loader answer and the
loader overlay allows the reader to read the file, the last stderr line is the reason; 69 no answer and the file is not
allowed (or the ledger is unreadable, or an overlay is missing or malformed), the stderr tail names the reason.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.adapters import (
    InMemoryLoaderSettingsSink,
    JournalLoaderSettingsSink,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers import (
    HandlerLoaderRun,
    HandlerLoaderSettings,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderRunStatus,
    EnumLoaderSettingsStatus,
    ModelLoaderRunRequest,
    ModelLoaderSettingsRequest,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.overlay import (
    load_hosts_overlay,
    load_loader_overlay,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.protocols import (
    ProtocolLoaderSettingsEventSink,
)

EXIT_RESOLVED = 0
EXIT_HOST_ROW_MISSING = 10
EXIT_REFUSED = 69
EXIT_RUN_ANSWERED = 0
EXIT_RUN_FILE_FALLBACK_ANSWERED = 10
EXIT_RUN_FALLBACK_ALLOWED = 11
EXIT_RUN_UNREADABLE = 69

_RUN_EXIT = {
    EnumLoaderRunStatus.ANSWERED: EXIT_RUN_ANSWERED,
    EnumLoaderRunStatus.FILE_FALLBACK_ANSWERED: EXIT_RUN_FILE_FALLBACK_ANSWERED,
    EnumLoaderRunStatus.FALLBACK_ALLOWED: EXIT_RUN_FALLBACK_ALLOWED,
    EnumLoaderRunStatus.UNREADABLE: EXIT_RUN_UNREADABLE,
}
_EXIT = {
    EnumLoaderSettingsStatus.RESOLVED: EXIT_RESOLVED,
    EnumLoaderSettingsStatus.HOST_ROW_MISSING: EXIT_HOST_ROW_MISSING,
    EnumLoaderSettingsStatus.REFUSED: EXIT_REFUSED,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    hostname: Callable[[], str] = socket.gethostname,
    run: Callable[..., Any] = subprocess.run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_ledger_rows_loader_settings_effect")
    parser.add_argument(
        "--registry-root", required=True, help="this host's registry root"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="run the host's loader with the remaining arguments and exit by what the reader does",
    )
    args, loader_args = parser.parse_known_args(argv)
    if loader_args and not args.run:
        parser.error(f"unrecognized arguments: {' '.join(loader_args)}")
    env = os.environ if environ is None else environ
    sink: ProtocolLoaderSettingsEventSink = InMemoryLoaderSettingsSink()
    try:
        journal = env.get(load_loader_overlay(env).journal_env, "").strip()
    except ValueError:
        journal = ""  # the handler refuses the same overlay below, naming it
    if journal:
        sink = JournalLoaderSettingsSink(Path(journal))
    settings = HandlerLoaderSettings(
        lambda: load_loader_overlay(env),
        lambda: load_hosts_overlay(env),
        sink,
        clock=clock,
    )
    host = hostname().split(".")[0]
    if args.run:
        outcome = HandlerLoaderRun(settings, run=run).handle(
            ModelLoaderRunRequest(
                registry_root=args.registry_root,
                host=host,
                loader_args=tuple(loader_args),
            )
        )
        sys.stdout.write(outcome.stdout)
        sys.stderr.write(outcome.stderr)
        if outcome.status not in (
            EnumLoaderRunStatus.ANSWERED,
            EnumLoaderRunStatus.FILE_FALLBACK_ANSWERED,
        ):
            sys.stderr.write(f"{outcome.reason}\n")
        return _RUN_EXIT[outcome.status]
    result = settings.handle(
        ModelLoaderSettingsRequest(registry_root=args.registry_root, host=host)
    )
    sys.stdout.write(json.dumps(result.payload()) + "\n")
    if result.status is not EnumLoaderSettingsStatus.RESOLVED:
        sys.stderr.write(
            f"LOADER-SETTINGS status={result.status.value} reason={result.reason}\n"
        )
    return _EXIT[result.status]
