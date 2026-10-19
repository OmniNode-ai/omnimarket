# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read an installed compaction out of a session transcript (TA.20b).

After a compaction is installed, the session transcript holds a
``compact_boundary`` record followed by the replacement records. The onex
plugin's ``hook_content_capture.py --kind compact``, run at PostCompact, asks
this module for the newest boundary and every record after it, exactly as the
harness wrote them, and journals that as one ``compact`` content item. The
capture owns the opt-out (``OMNICLAUDE_CONTENT_CAPTURE=0``), the drainer
refusal, the size policy and the topic; this module only reads.

A record is a boundary only when it is a ``system`` entry whose ``subtype`` is
``compact_boundary``: the literal quoted inside a tool result is not one.
PostCompact can fire before the harness has flushed the boundary and the
replacement records, so ``package_when_flushed`` polls briefly.

Where it is loaded from
-----------------------
The capture loads this file BY PATH, not through the ``omnimarket.nodes``
package: a package import runs ``__init__`` modules along the way, a cost no
per-tool-call hook can pay. So the module level imports the standard library
only; the node's pydantic models are imported inside
:meth:`HandlerCompactPackageRead.handle`, which only the runtime calls. A test
pins that module-level import set.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnimarket.nodes.node_compact_package_read_effect.models import (
        ModelCompactPackageReadRequest,
        ModelCompactPackageReadResult,
    )

#: A transcript's last bytes are read to find the boundary; a compaction's
#: replacement records follow it directly, so the newest one sits near the tail.
TRANSCRIPT_TAIL_BYTES = 8 * 1024 * 1024

TRANSCRIPT_POLL_ATTEMPTS = 6
TRANSCRIPT_POLL_SECONDS = 0.5


def _tail_lines(transcript: Path, tail_bytes: int) -> list[str]:
    """The transcript's last ``tail_bytes`` as lines; ``[]`` when unreadable."""
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()  # drop the partial first line
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def compact_package(
    transcript: Path, *, tail_bytes: int = TRANSCRIPT_TAIL_BYTES
) -> str:
    """The newest ``compact_boundary`` line and every line after it, verbatim.

    The lines are kept exactly as the harness wrote them (no parse and
    re-serialise), each ending in a newline. Returns ``""`` when the tail holds
    no boundary, including when the transcript is absent or unreadable.
    """
    lines = _tail_lines(transcript, tail_bytes)
    for index in range(len(lines) - 1, -1, -1):
        # Cheap pre-filter: a boundary line names its subtype literally.
        if '"compact_boundary"' not in lines[index]:
            continue
        try:
            entry = json.loads(lines[index])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("type") == "system"
            and entry.get("subtype") == "compact_boundary"
        ):
            return "\n".join(lines[index:]) + "\n"
    return ""


def package_when_flushed(
    transcript: Path,
    *,
    tail_bytes: int = TRANSCRIPT_TAIL_BYTES,
    attempts: int = TRANSCRIPT_POLL_ATTEMPTS,
    pause_s: float = TRANSCRIPT_POLL_SECONDS,
) -> str:
    """``compact_package``, read again after a pause until it finds one or ``attempts`` run out."""
    for attempt in range(max(1, attempts)):
        package = compact_package(transcript, tail_bytes=tail_bytes)
        if package:
            return package
        if attempt + 1 < attempts:
            time.sleep(pause_s)
    return ""


def compact_items(hook_input: dict[str, Any]) -> list[dict[str, Any]]:
    """The capture's content items for a PostCompact hook input.

    One ``compact`` item holding the package, or none when the input names no
    transcript or the transcript holds no boundary.
    """
    path = hook_input.get("transcript_path") or hook_input.get("transcriptPath")
    package = package_when_flushed(Path(path)) if isinstance(path, str) and path else ""
    return [{"content_kind": "compact", "content": package}] if package else []


class HandlerCompactPackageRead:
    """The node's handler: one transcript in, the newest compaction out."""

    def handle(
        self, request: ModelCompactPackageReadRequest
    ) -> ModelCompactPackageReadResult:
        from omnimarket.nodes.node_compact_package_read_effect.models import (
            ModelCompactPackageReadResult,
        )

        package = package_when_flushed(
            Path(request.transcript_path),
            tail_bytes=request.tail_bytes,
            attempts=request.attempts,
            pause_s=request.pause_s,
        )
        if not package:
            return ModelCompactPackageReadResult(
                found=False,
                package="",
                detail="no compact_boundary record in the transcript",
            )
        return ModelCompactPackageReadResult(
            found=True,
            package=package,
            detail=f"{package.count(chr(10))} record(s) from the newest boundary",
        )


__all__ = [
    "TRANSCRIPT_POLL_ATTEMPTS",
    "TRANSCRIPT_POLL_SECONDS",
    "TRANSCRIPT_TAIL_BYTES",
    "HandlerCompactPackageRead",
    "compact_items",
    "compact_package",
    "package_when_flushed",
]
