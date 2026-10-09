# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the ledger-reconcile effect handlers act through (OMN-20677).

Every handler takes its ports in its constructor, so the golden and error chains run
against fakes and the deployment wires the local adapters
(``local_ledger_reconcile_adapters``). A port that cannot answer raises
``ReconcilePortError``; the handler turns it into a result that says what failed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from omnimarket.models.ledger_reconcile import (
    ModelPrFact,
    ModelPushFact,
    ModelReconcileOverlay,
    ModelReconcileSource,
    ModelShaFact,
    ModelShaRef,
)


class ReconcilePortError(RuntimeError):
    """A port could not read or act; the message names what failed."""


class ProtocolReconcileHost(Protocol):
    """The operator's files: ledger, archives, clone registry, roster and overlay."""

    def prerequisites(self) -> str:
        """Why a reconciliation cannot run on this host, or ''."""
        ...

    def ledger_path(self) -> Path: ...

    def registry_root(self) -> Path: ...

    def read_ledger(
        self, ledger: Path
    ) -> tuple[ModelReconcileSource, tuple[ModelReconcileSource, ...]]: ...

    def clone_names(self, root: Path) -> tuple[str, ...]: ...

    def read_roster(self, path: Path) -> frozenset[str]: ...

    def overlay(self) -> ModelReconcileOverlay: ...


class ProtocolReconcileGitHub(Protocol):
    def pr(self, org: str, repo: str, number: int) -> ModelPrFact: ...

    def pushed_at(self, org: str, repo: str, number: int) -> ModelPushFact: ...


class ProtocolReconcileGit(Protocol):
    def probe(
        self, root: Path, registry_name: str, ref: ModelShaRef
    ) -> ModelShaFact: ...


class ProtocolReconcileAppender(Protocol):
    def append(self, ledger: Path, row: str) -> str:
        """Append one row; '' on success, else the error text."""
        ...


class ProtocolReconcileClock(Protocol):
    def now(self) -> datetime: ...
