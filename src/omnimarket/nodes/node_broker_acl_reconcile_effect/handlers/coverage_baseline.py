# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the frozen coverage baseline the merge check holds each node at (OMN-24005).

The reconcile effect owns the file read; the coverage gate handler stays pure and takes the baseline on its request.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.broker_acl import ModelCoverageBaselineEntry


def load_coverage_baseline(path: Path) -> tuple[ModelCoverageBaselineEntry, ...]:
    """The baseline entries, sorted by node; a missing file, a non-mapping, a missing key or a bad value raises naming the path."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"{path}: {error}") from error
    loaded = yaml.load(
        text,
        Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
    )
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: baseline must be a mapping")
    nodes = loaded.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError(f"{path}: missing 'nodes' mapping")
    entries: list[ModelCoverageBaselineEntry] = []
    for name, value in nodes.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{path}: {name} must be a positive integer")
        entries.append(ModelCoverageBaselineEntry(node=name, uncovered_uses=value))
    return tuple(sorted(entries, key=lambda entry: entry.node))
