# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five seams of the quarantine node: store, repository, gates, clock, report."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_code_quarantine_effect.models import (
    ModelCatalogRow,
    ModelQuarantineEvent,
    ModelQuarantineRecord,
)


class QuarantineStepError(Exception):
    """A seam could not do its step. ``step`` is ``store``, ``repository``, ``gate`` or ``report``."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class ProtocolQuarantineStore(Protocol):
    def get(self, repository: str, entity: str) -> ModelQuarantineRecord | None: ...

    def put(self, record: ModelQuarantineRecord) -> None: ...

    def excluded_paths(self, repository: str) -> tuple[str, ...]:
        """Every path (entity and test) that is out of the build in the repository, sorted."""

    def append_catalog(
        self, repository: str, rows: tuple[ModelCatalogRow, ...]
    ) -> None: ...


class ProtocolRepository(Protocol):
    def path_exists(self, repository: str, path: str) -> bool: ...

    def tag_sha(self, repository: str, tag: str) -> str:
        """The sha the tag names, empty when the repository has no such tag."""

    def remove_paths(self, repository: str, paths: tuple[str, ...]) -> None: ...


class ProtocolGates(Protocol):
    def clean_build(self, repository: str, excluded: tuple[str, ...]) -> bool:
        """True when a clean build of the repository, without the excluded paths, is green."""

    def lab_runtime_start(self, repository: str, excluded: tuple[str, ...]) -> bool:
        """True when the lab runtime starts without the excluded paths."""

    def reading_present(self, repository: str, entity: str) -> bool:
        """True when the entity's reading exists; a missing reading is False."""


class ProtocolClock(Protocol):
    def now(self) -> datetime: ...


class ProtocolQuarantineReporter(Protocol):
    def report(self, event: ModelQuarantineEvent) -> None: ...
