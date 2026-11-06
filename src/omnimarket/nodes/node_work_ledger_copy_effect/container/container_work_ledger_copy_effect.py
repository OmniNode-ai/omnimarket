# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binds the copy handler to its real seams from the overlay."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_work_ledger_copy_effect.adapters.adapters_real import (
    AppendReporter,
    DockerDrill,
    PostgresSource,
    SshStore,
)
from omnimarket.nodes.node_work_ledger_copy_effect.handlers.handler_work_ledger_copy import (
    HandlerWorkLedgerCopy,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import ModelCopyOverlay


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


__all__ = ["create_handler_work_ledger_copy"]
