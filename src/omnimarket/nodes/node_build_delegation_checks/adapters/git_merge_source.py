# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merges between two builds, read from the first-parent history of a canonical clone (a read; nothing is fetched)."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.protocols.protocol_build_checks import (
    UnresolvableRangeError,
)

_PR = re.compile(r"\(#(\d+)\)\s*$")


def parse_log(text: str) -> tuple[ModelAttributionCandidate, ...]:
    """Parse ``%H|%cI|%s`` lines, newest first as git prints them, into candidates oldest first."""
    found: list[ModelAttributionCandidate] = []
    for line in text.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].strip():
            continue
        sha, committed, subject = parts
        match = _PR.search(subject)
        found.append(
            ModelAttributionCandidate(
                commit_sha=sha,
                pr_number=int(match.group(1)) if match else None,
                merged_at=datetime.fromisoformat(committed),
            )
        )
    return tuple(reversed(found))


class GitMergeSource:
    def __init__(self, clone: Path, timeout_seconds: float = 60.0) -> None:
        self._clone = clone
        self._timeout = timeout_seconds

    def merges_between(
        self, base_sha: str, head_sha: str
    ) -> tuple[ModelAttributionCandidate, ...]:
        done = subprocess.run(
            [
                "git",
                # The runtime mounts the clone read-only under another owner, where git refuses it as dubious ownership; the
                # clone is named safe for this one read only, never through a global or wildcard setting.
                "-c",
                f"safe.directory={self._clone}",
                "-C",
                str(self._clone),
                "log",
                "--first-parent",
                "--format=%H|%cI|%s",
                f"{base_sha}..{head_sha}",
            ],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if done.returncode != 0:
            message = (
                f"git log {base_sha[:12]}..{head_sha[:12]} failed: rc={done.returncode}"
            )
            if done.returncode == 128:
                raise UnresolvableRangeError(message)
            raise OSError(message)
        return parse_log(done.stdout)


__all__ = ["GitMergeSource", "UnresolvableRangeError", "parse_log"]
