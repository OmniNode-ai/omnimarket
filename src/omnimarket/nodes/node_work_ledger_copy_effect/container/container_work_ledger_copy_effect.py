# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binds the copy handler to its real seams from the overlay."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_real import (
    AppendReporter,
    DockerDrill,
    PostgresSource,
    Runner,
    SshStore,
    run_command,
)
from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_snapshot import (
    AppendDrillReporter,
    CommandRowSource,
    CommandSourceProbe,
    DirectorySnapshotStore,
    SqliteTableRebuilder,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers.handler_work_ledger_copy import (
    HandlerWorkLedgerCopy,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers.handler_work_ledger_snapshot import (
    HandlerWorkLedgerRestoreDrill,
    HandlerWorkLedgerSnapshot,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    ModelCopyOverlay,
    ModelSnapshotOverlay,
)


def create_handler_work_ledger_copy(
    overlay: ModelCopyOverlay, environ: Mapping[str, str]
) -> HandlerWorkLedgerCopy:
    return HandlerWorkLedgerCopy(
        source=PostgresSource(overlay, environ),
        store=SshStore(overlay),
        drill=DockerDrill(overlay),
        reporter=AppendReporter(overlay, environ=dict(environ)),
        keep=overlay.target.keep,
        min_free_bytes=overlay.target.min_free_bytes,
    )


def create_handler_work_ledger_snapshot(
    overlay: ModelSnapshotOverlay,
    environ: Mapping[str, str],
    run: Runner = run_command,
) -> HandlerWorkLedgerSnapshot:
    return HandlerWorkLedgerSnapshot(
        source_host=overlay.source.host,
        store_host=overlay.store.host,
        topic=CommandRowSource(
            overlay.source.topic_export_argv,
            overlay.source.export_timeout_seconds,
            run=run,
            environ=dict(environ),
        ),
        projection=CommandRowSource(
            overlay.source.projection_export_argv,
            overlay.source.export_timeout_seconds,
            run=run,
            environ=dict(environ),
        ),
        store=DirectorySnapshotStore(overlay.store.directory),
        keep=overlay.store.keep,
    )


def create_handler_work_ledger_restore_drill(
    overlay: ModelSnapshotOverlay,
    environ: Mapping[str, str],
    run: Runner = run_command,
) -> HandlerWorkLedgerRestoreDrill:
    return HandlerWorkLedgerRestoreDrill(
        drill_host=overlay.drill.host,
        source_host=overlay.source.host,
        source_port=overlay.source.port,
        store=DirectorySnapshotStore(overlay.store.directory),
        probe=CommandSourceProbe(
            overlay.drill.reachability_argv,
            overlay.drill.probe_timeout_seconds,
            run=run,
        ),
        rebuilder=SqliteTableRebuilder(overlay.drill.scratch_table),
        reporter=AppendDrillReporter(
            overlay,
            overlay.drill.probe_timeout_seconds,
            run=run,
            environ=dict(environ),
        ),
    )


__all__ = [
    "create_handler_work_ledger_copy",
    "create_handler_work_ledger_restore_drill",
    "create_handler_work_ledger_snapshot",
]
