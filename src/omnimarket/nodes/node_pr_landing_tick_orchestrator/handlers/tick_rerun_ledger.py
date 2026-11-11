# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rerun ledgers of the landing tick orchestrator: in-memory, and one JSON object per line (OMN-23830).

The landing tick grants re-runs; the ledger holds them so a later tick, a new process, still
answers a repeat request as a duplicate. Grants are kept per pull request key (owner/repo#n):
granted() returns only that pull request's grants. The file ledger appends one JSON object per
line and reads them back without locking or fsync.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_pr_landing_rerun_request_compute.models.model_rerun_decide_input import (
    ModelRerunGrant,
)


class ProtocolRerunLedger(Protocol):
    """The re-runs the landing tick has granted, kept per pull request key."""

    def granted(self, pr: str) -> tuple[ModelRerunGrant, ...]:
        """The grants already recorded for the pull request, any head."""
        ...

    def record(self, pr: str, grant: ModelRerunGrant) -> None:
        """Record one more grant for the pull request."""
        ...


class InMemoryRerunLedger:
    """The in-memory default: grants kept per pull request key in a dict."""

    def __init__(self) -> None:
        self._grants: dict[str, list[ModelRerunGrant]] = {}

    def granted(self, pr: str) -> tuple[ModelRerunGrant, ...]:
        return tuple(self._grants.get(pr, ()))

    def record(self, pr: str, grant: ModelRerunGrant) -> None:
        self._grants.setdefault(pr, []).append(grant)


class FileRerunLedger:
    """The file path: one JSON object per line, {"pr", "head_sha", "job"}, appended per grant."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def granted(self, pr: str) -> tuple[ModelRerunGrant, ...]:
        if not self._path.exists():
            return ()
        grants: list[ModelRerunGrant] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"rerun ledger line {number} in {self._path} is not JSON: {error}"
                    ) from error
                if (
                    not isinstance(entry, dict)
                    or not {
                        "pr",
                        "head_sha",
                        "job",
                    }
                    <= entry.keys()
                ):
                    raise ValueError(
                        f"rerun ledger line {number} in {self._path} lacks the keys "
                        "pr, head_sha, job"
                    )
                if entry["pr"] == pr:
                    grants.append(
                        ModelRerunGrant(head_sha=entry["head_sha"], job=entry["job"])
                    )
        return tuple(grants)

    def record(self, pr: str, grant: ModelRerunGrant) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"pr": pr, "head_sha": grant.head_sha, "job": grant.job}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


__all__: list[str] = [
    "FileRerunLedger",
    "InMemoryRerunLedger",
    "ProtocolRerunLedger",
]
