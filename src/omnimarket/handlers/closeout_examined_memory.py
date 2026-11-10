# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's examination memory on disk: one JSON object, ticket id to its last selection
(OMN-23282). The pure rules that read it are in the node (closer_examined_rules), which does no I/O; this is the real backend, beside the other closeout boundary reads.
An absent file is an empty memory (the first run). A file that is not the memory shape is refused
by name: silently starting over would bring back the picks of the same tickets every fire."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    ScopeInputUnreadableError,
)


def read_examined_memory(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ScopeInputUnreadableError(f"{path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ScopeInputUnreadableError(f"{path}: not JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in data.items()
    ):
        raise ScopeInputUnreadableError(
            f"{path}: not an object of ticket id to entry; move it aside to start the memory over"
        )
    return data


def write_examined_memory(path: Path, memory: dict[str, dict[str, Any]]) -> None:
    """Replace the file whole: a reader sees the old memory or the new, never a part."""
    temp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(memory, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    except OSError as exc:
        if temp is not None:
            temp.unlink(missing_ok=True)
        raise ScopeInputUnreadableError(f"{path}: not written: {exc}") from exc
