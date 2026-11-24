# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What changed between two probe runs: ledger rows that record a host or model setting change, merges, dev builds."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumChangeKind,
    ModelChange,
    ModelChangeMarker,
    ModelMergeSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolChangeSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

GitRunner = Callable[[Sequence[str]], str]


class LedgerMarkerChangeSource:
    """A ledger row of a marker's type that names every marker term is one host or model setting change."""

    def __init__(
        self, rows: ProtocolLedgerRowSource, markers: Sequence[ModelChangeMarker]
    ) -> None:
        self._rows = rows
        self._markers = tuple(markers)

    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        changes: list[ModelChange] = []
        for marker in self._markers:
            answer = self._rows.read(
                ModelLedgerQuery(since=start, until=end, row_type=marker.row_type)
            )
            if answer.error is not None:
                raise OSError(f"ledger read: {answer.error}")
            for row in answer.rows:
                if not start < row.stamp <= end:
                    continue
                if all(term in row.raw for term in marker.contains_all):
                    summary = (
                        f"{marker.row_type} row {row.stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                        f"names {', '.join(marker.contains_all)}"
                    )
                    changes.append(
                        ModelChange(
                            kind=marker.kind,
                            at=row.stamp,
                            ref=row.cells.get("req", ""),
                            summary=summary,
                        )
                    )
        return tuple(changes)


def run_git(argv: Sequence[str]) -> str:
    done = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=60, check=False
    )
    if done.returncode != 0:
        raise OSError(f"git exited {done.returncode}: {done.stderr.strip()[:160]}")
    return done.stdout


class GitMergeChangeSource:
    """The squash merges of a branch's first-parent history in the window, read from the canonical clone's remote ref."""

    def __init__(
        self,
        registry_root: Path,
        sources: Sequence[ModelMergeSource],
        runner: GitRunner = run_git,
    ) -> None:
        self._root = registry_root
        self._sources = tuple(sources)
        self._run = runner

    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        changes: list[ModelChange] = []
        for source in self._sources:
            out = self._run(
                [
                    "git",
                    "-C",
                    str(self._root / source.clone),
                    "log",
                    "--first-parent",
                    f"origin/{source.branch}",
                    f"--since={(start + timedelta(seconds=1)).astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    f"--until={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    "--format=%H%x09%cI%x09%s",
                ]
            )
            for line in out.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                at = datetime.fromisoformat(parts[1]).astimezone(UTC)
                if start < at <= end:
                    changes.append(
                        ModelChange(
                            kind=EnumChangeKind.MERGE,
                            at=at,
                            ref=f"{source.repository}@{parts[0][:12]}",
                            summary=parts[2][:160],
                        )
                    )
        return tuple(changes)


class _Between(Protocol):
    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]: ...


class CompositeChangeSource:
    """Every source's changes in time order. A source that cannot be read leaves its gap named, never an empty answer."""

    def __init__(self, sources: Sequence[_Between]) -> None:
        self._sources = tuple(sources)

    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        changes: list[ModelChange] = []
        for source in self._sources:
            try:
                changes.extend(source.between(start, end))
            except OSError as exc:
                changes.append(
                    ModelChange(
                        kind=EnumChangeKind.MERGE,
                        at=end,
                        summary=f"{type(source).__name__} unreadable: {str(exc)[:120]}",
                    )
                )
        return tuple(sorted(changes, key=lambda c: c.at))


_ = ProtocolChangeSource
