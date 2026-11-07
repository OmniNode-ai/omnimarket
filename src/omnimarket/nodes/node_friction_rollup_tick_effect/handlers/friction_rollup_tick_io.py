# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file reads and writes of the friction rollup tick (OMN-22987): sources, atomic writes, pruning, class rules."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.events.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_core import (
    STAMPED_REPORT,
    previous_document,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickSettings,
)


def display_archive_path(ledger_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(ledger_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def read_sources(
    ledger: Path, archive_dir: Path, settings: ModelFrictionRollupTickSettings
) -> list[ModelFrictionRollupSource]:
    """Whole texts in live ledger then sorted archive order; a missing archive directory is a missing source."""
    sources = [
        ModelFrictionRollupSource(
            display=ledger.name,
            kind="markdown",
            text=ledger.read_text(encoding="utf-8"),
        )
    ]
    if archive_dir.is_dir():
        for path in sorted(archive_dir.glob(settings.archive_glob)):
            sources.append(
                ModelFrictionRollupSource(
                    display=display_archive_path(ledger.parent, path),
                    kind="markdown",
                    text=path.read_text(encoding="utf-8"),
                )
            )
    else:
        sources.append(
            ModelFrictionRollupSource(
                display=display_archive_path(ledger.parent, archive_dir),
                kind="missing",
                text=None,
            )
        )
    return sources


def read_previous(
    path: Path, settings: ModelFrictionRollupTickSettings
) -> dict[str, Any] | None:
    """The previous tick's document, or None when absent, unreadable or foreign."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return previous_document(payload, settings)


def write_atomic(path: Path, text: str) -> None:
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def prune_reports(state_dir: Path, now: datetime, keep_days: int) -> list[Path]:
    """Remove stamped reports older than ``keep_days``; latest.* is never touched."""
    cutoff = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for path in sorted(state_dir.iterdir()):
        match = STAMPED_REPORT.match(path.name)
        if match is None:
            continue
        stamped = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
        if stamped < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def load_settings(path: Path) -> ModelFrictionRollupTickSettings:
    """The tick's settings from an overlay file."""
    import yaml

    return ModelFrictionRollupTickSettings.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_class_rules(
    path: Path,
) -> tuple[list[ModelFrictionClassRule], list[ModelFrictionClassOwner]]:
    """The class rules and class owners of the rules file; raises when it cannot be read or is malformed."""
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("rules file is not a mapping")
    rules = [
        ModelFrictionClassRule.model_validate(item)
        for item in document.get("class_rules") or []
    ]
    owners = [
        ModelFrictionClassOwner.model_validate(item)
        for item in document.get("class_owners") or []
    ]
    return rules, owners
