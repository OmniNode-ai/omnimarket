# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write one spool entry and the lane's unread marker, atomically and once.

Layout under the lane directory: ``msgs/<row_id>.json`` holds an undelivered
entry, ``delivered/<row_id>.json`` an entry the hook shim has injected, and
``unread`` exists while ``msgs/`` holds an entry. A row id found in either
directory is a duplicate, so a replay never re-raises the marker for a MSG the
lane has already seen.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import ModelAgentInboxSpoolEntry

MSGS_DIR = "msgs"
DELIVERED_DIR = "delivered"
UNREAD_MARKER = "unread"


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def spool_entry(spool_root: Path, lane: str, entry: ModelAgentInboxSpoolEntry) -> bool:
    """Return True when the entry was written, False when it was already spooled."""
    lane_dir = spool_root / lane
    msgs, delivered = lane_dir / MSGS_DIR, lane_dir / DELIVERED_DIR
    name = f"{entry.row_id}.json"
    if (msgs / name).exists() or (delivered / name).exists():
        return False
    msgs.mkdir(parents=True, exist_ok=True)
    _atomic_write(msgs / name, entry.model_dump_json() + "\n")
    # The marker follows the entry, so a reader that sees it finds the entry.
    _atomic_write(lane_dir / UNREAD_MARKER, entry.row_id + "\n")
    return True
