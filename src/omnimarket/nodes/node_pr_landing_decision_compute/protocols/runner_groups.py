# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner-group read port of J8 (OMN-22919): in memory by default, a saved snapshot's text as the real backend.

The text adapter parses the snapshot a collector saved from the organisation's runner-group listing; the node does no
file I/O (it is a compute node), so the effect that reads the file hands its text over. Unparseable or malformed text is
refused naming its source; it is never read as an empty (clean) snapshot.
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_check import (
    ModelRunnerGroupSnapshot,
)


class ProtocolRunnerGroupSource(Protocol):
    def read(self) -> ModelRunnerGroupSnapshot: ...


class InMemoryGroupSnapshotSource:
    def __init__(self, snapshot: ModelRunnerGroupSnapshot) -> None:
        self._snapshot = snapshot

    def read(self) -> ModelRunnerGroupSnapshot:
        return self._snapshot


def parse_runner_group_snapshot(text: str, source: str) -> ModelRunnerGroupSnapshot:
    """Parse a saved snapshot's text; unparseable or malformed text is refused naming the source, never read as clean."""
    try:
        return ModelRunnerGroupSnapshot.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise ValueError(
            f"runner group snapshot {source} is unreadable: {exc}"
        ) from exc


class TextGroupSnapshotSource:
    """The real backend's seam: an effect reads the collector's saved file and hands its text here."""

    def __init__(self, text: str, source: str) -> None:
        self._text = text
        self._source = source

    def read(self) -> ModelRunnerGroupSnapshot:
        return parse_runner_group_snapshot(self._text, self._source)
