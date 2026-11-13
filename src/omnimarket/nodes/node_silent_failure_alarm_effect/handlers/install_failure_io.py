# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the install-failures rows and keep the install-failure alarm's state (OMN-21663).

The rows are the landing controller's ``install-failures.jsonl``: one JSON object per line with ``ts`` and
``detail`` and, for a refusal, ``class``. A line that is not such an object is skipped and named in a warning, so a
damaged file is reported and never read as no failures.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelInstallFailureRow,
    ModelInstallFailureState,
)

from .alarm_io import one_line


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_failures(
    path: Path,
) -> tuple[tuple[ModelInstallFailureRow, ...], bool, tuple[str, ...]]:
    """The rows, whether the file could be read, and one warning per skipped line. A missing file is no rows."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), True, ()
    except (OSError, UnicodeError) as exc:
        return (), False, (one_line(exc, "install-failures file unreadable"),)
    rows: list[ModelInstallFailureRow] = []
    warnings: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        at = _stamp(item.get("ts")) if isinstance(item, dict) else None
        if at is None or not isinstance(item, dict):
            warnings.append(
                f"install-failures row {number} is not a timestamped object"
            )
            continue
        rows.append(
            ModelInstallFailureRow(
                at=at,
                detail=str(item.get("detail", "")),
                failure_class=str(item.get("class", "")),
            )
        )
    return tuple(rows), True, tuple(warnings)


def read_state(path: Path) -> ModelInstallFailureState:
    """The state last written; a missing or damaged file reads as no raise yet."""
    try:
        return ModelInstallFailureState.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return ModelInstallFailureState()


def write_state(path: Path, state: ModelInstallFailureState) -> str:
    """Write the state atomically; the problem as one line, or an empty string."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(state.model_dump(mode="json"), handle)
            handle.write("\n")
        temporary.replace(path)
    except OSError as exc:
        return one_line(exc, "install-failure state write failed")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ""
