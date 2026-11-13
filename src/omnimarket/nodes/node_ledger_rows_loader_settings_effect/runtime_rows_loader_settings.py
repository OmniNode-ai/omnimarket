# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_rows_loader_settings_effect``: this host's rows loader settings.

Resolves the loader command of the host it runs on from the ``ledger_rows_loader_hosts`` overlay and the
loader overlay's fallback flag (overlays found under ``ONEX_SKILL_OVERLAY_ROOTS``), prints one JSON line on
stdout and, when no row was found or an overlay was refused, a ``LOADER-SETTINGS`` line on stderr that names
the reason. The caller names its registry root with ``--registry-root``. Exit 0: the host's row was found. Exit
10: no row for this host (the JSON still carries the overlay's fallback flag). Exit 69: an overlay is missing or
malformed; the stderr line names it.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.adapters import (
    InMemoryLoaderSettingsSink,
    JournalLoaderSettingsSink,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers import (
    HandlerLoaderSettings,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderSettingsStatus,
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
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_ledger_rows_loader_settings_effect")
    parser.add_argument(
        "--registry-root", required=True, help="this host's registry root"
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    sink: ProtocolLoaderSettingsEventSink = InMemoryLoaderSettingsSink()
    try:
        journal = env.get(load_loader_overlay(env).journal_env, "").strip()
    except ValueError:
        journal = ""  # the handler refuses the same overlay below, naming it
    if journal:
        sink = JournalLoaderSettingsSink(Path(journal))
    result = HandlerLoaderSettings(
        lambda: load_loader_overlay(env),
        lambda: load_hosts_overlay(env),
        sink,
        clock=clock,
    ).handle(
        ModelLoaderSettingsRequest(
            registry_root=args.registry_root, host=hostname().split(".")[0]
        )
    )
    sys.stdout.write(json.dumps(result.payload()) + "\n")
    if result.status is not EnumLoaderSettingsStatus.RESOLVED:
        sys.stderr.write(
            f"LOADER-SETTINGS status={result.status.value} reason={result.reason}\n"
        )
    return _EXIT[result.status]
