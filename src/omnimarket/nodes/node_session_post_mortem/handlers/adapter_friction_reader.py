# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""adapter_friction_reader — reads .onex_state/friction/ directory.

Best-effort JSON parsing:
- Valid JSON with expected fields → ModelFrictionEvent
- Markdown or unparseable → synthetic event with friction_type="raw_file"
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

from omnimarket.nodes.node_session_post_mortem.models.model_post_mortem_report import (
    ModelFrictionEvent,
)

logger = logging.getLogger(__name__)


def read_friction_events(friction_dir: str) -> list[ModelFrictionEvent]:
    """Read friction events from friction_dir.

    Scans *.json and *.md files. Returns parsed ModelFrictionEvent instances.
    Failures in individual files are caught and converted to synthetic events.

    Args:
        friction_dir: Path to the friction event directory.

    Returns:
        List of ModelFrictionEvent instances (may be empty).
    """
    events: list[ModelFrictionEvent] = []

    if not os.path.isdir(friction_dir):
        logger.debug("Friction dir does not exist: %s", friction_dir)
        return events

    for filename in sorted(os.listdir(friction_dir)):
        if not (filename.endswith(".json") or filename.endswith(".md")):
            continue

        filepath = os.path.join(friction_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            logger.warning("Could not read friction file %s: %s", filepath, exc)
            continue

        event = _parse_friction_file(filename, content)
        if event is not None:
            events.append(event)

    return events


def _parse_friction_file(filename: str, content: str) -> ModelFrictionEvent | None:
    """Parse a single friction file into a ModelFrictionEvent.

    Tries JSON parse first; falls back to synthetic raw_file event.
    """
    if filename.endswith(".json"):
        try:
            data = json.loads(content)
            return ModelFrictionEvent(
                event_id=str(data.get("event_id", uuid.uuid4())),
                ticket_id=data.get("ticket_id"),
                agent_id=data.get("agent_id"),
                friction_type=str(data.get("friction_type", "unknown")),
                description=str(data.get("description", filename)),
                recorded_at=datetime.fromisoformat(data["recorded_at"])
                if "recorded_at" in data
                else datetime.now(UTC),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug("Could not parse JSON friction file %s: %s", filename, exc)

    # Fallback: synthetic raw_file event
    first_line = content.splitlines()[0].strip() if content.strip() else filename
    return ModelFrictionEvent(
        event_id=str(uuid.uuid4()),
        friction_type="raw_file",
        description=first_line[:500],
        recorded_at=datetime.now(UTC),
    )


__all__: list[str] = ["read_friction_events"]
