# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The log lines a cycle's source-cache receipts become: one per board step, in receipt-name order.

Each board step writes its tally receipt beside the registry's other state, but nothing printed it, so the
publisher's log never showed a cache hit or a miss. ``read_tally_lines`` turns the receipts written at or after a
cycle's start into the ``source-cache:`` lines the publisher logs. A receipt that cannot be read or parsed never
stops the listing: it becomes a line naming the exception's class, never a message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_source_read_cache import (
    ModelSourceCacheTally,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    OWN_CONTRACT,
)

RECEIPT_GLOB = "*.json"
RECEIPT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LINE_PREFIX = "source-cache:"
_OFF_NONE = "none"
# The receipt carries the tally plus these two keys; only the tally is a ModelSourceCacheTally.
_RECEIPT_EXTRA_KEYS = ("step", "written_at")


def _receipt_dir(home: Path, contract_path: Path) -> Path:
    """Where the receipts are written, from the contract's runtime block, under the registry home."""
    runtime = yaml.safe_load(contract_path.read_text(encoding="utf-8"))["runtime"]
    return home / str(runtime["receipt_dir_relpath"])


def _utc(moment: datetime) -> datetime:
    """``moment`` as aware UTC; a naive one is read as UTC, which is how the receipts are written."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _tally_line(step: str, tally: ModelSourceCacheTally) -> str:
    """The ``source-cache:`` line for one step's tally; a cache that is off says why after ``off=``."""
    off = tally.disabled_reason or _OFF_NONE
    return (
        f"{LINE_PREFIX} step={step} backend={tally.backend} hits={tally.hits}"
        f" misses={tally.misses} stores={tally.stores} bypassed={tally.bypassed}"
        f" unavailable={tally.unavailable} source_reads={tally.source_reads}"
        f" source_seconds={tally.source_seconds} off={off}"
    )


def read_tally_lines(
    home: Path,
    since: datetime,
    contract_path: Path = OWN_CONTRACT,
) -> list[str]:
    """One ``source-cache:`` line per receipt in ``home`` written at or after ``since``, in receipt-name order.

    A receipt written before ``since`` belongs to an earlier cycle and is skipped. A receipt that cannot be read or
    parsed, or that lacks ``written_at``, becomes the ``receipt unreadable`` line naming the exception's class only.
    """
    directory = _receipt_dir(home, contract_path)
    if not directory.is_dir():
        return []
    # A receipt's written_at is whole seconds, so a run's start is compared at whole seconds too.
    threshold = _utc(since).replace(microsecond=0)
    lines: list[str] = []
    for path in sorted(directory.glob(RECEIPT_GLOB), key=lambda receipt: receipt.name):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            step = document["step"]
            written_at = _utc(
                datetime.strptime(document["written_at"], RECEIPT_TIME_FORMAT)
            )
            tally = ModelSourceCacheTally.model_validate(
                {
                    field: value
                    for field, value in document.items()
                    if field not in _RECEIPT_EXTRA_KEYS
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            lines.append(
                f"{LINE_PREFIX} step={path.stem} receipt unreadable ({type(exc).__name__})"
            )
            continue
        if written_at < threshold:
            continue
        lines.append(_tally_line(step, tally))
    return lines
