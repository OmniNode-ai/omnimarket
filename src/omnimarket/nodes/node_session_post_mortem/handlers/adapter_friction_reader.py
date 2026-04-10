# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AdapterFrictionReader — reads friction events from .onex_state/friction/.

Best-effort JSON parsing:
- Valid JSON with expected fields → ModelFrictionEventLocal
- Markdown or unparseable → synthetic event with friction_type="raw_file"
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

from omnimarket.nodes.node_session_post_mortem.handlers.handler_session_post_mortem import (
    ModelFrictionEventLocal,
)

logger = logging.getLogger(__name__)


class AdapterFrictionReader:
    """Reads friction events from the friction directory on disk."""

    def read_friction_events(self, friction_dir: str) -> list[ModelFrictionEventLocal]:
        """Read all friction events from the given directory.

        Args:
            friction_dir: Path to .onex_state/friction/ directory.

        Returns:
            List of parsed ModelFrictionEventLocal instances. Never raises.
        """
        abs_dir = os.path.abspath(friction_dir)
        if not os.path.isdir(abs_dir):
            logger.debug("Friction dir %s does not exist — returning empty", abs_dir)
            return []

        events: list[ModelFrictionEventLocal] = []
        for filename in sorted(os.listdir(abs_dir)):
            filepath = os.path.join(abs_dir, filename)
            if not os.path.isfile(filepath):
                continue
            event = self._parse_file(filepath, filename)
            if event is not None:
                events.append(event)

        return events

    def _parse_file(
        self, filepath: str, filename: str
    ) -> ModelFrictionEventLocal | None:
        """Parse a single friction file. Returns None on unrecoverable error."""
        try:
            with open(filepath) as f:
                content = f.read()
        except OSError as exc:
            logger.warning("Could not read friction file %s: %s", filepath, exc)
            return None

        if filename.endswith(".json"):
            return self._parse_json(content, filename)
        return self._parse_raw(content, filename)

    def _parse_json(self, content: str, filename: str) -> ModelFrictionEventLocal:
        """Parse JSON friction file, falling back to raw on parse error."""
        try:
            data = json.loads(content)
            return ModelFrictionEventLocal(
                event_id=str(data.get("event_id", str(uuid.uuid4()))),
                ticket_id=data.get("ticket_id"),
                agent_id=data.get("agent_id"),
                friction_type=str(data.get("friction_type", "unknown")),
                description=str(data.get("description", filename)),
                recorded_at=datetime.fromisoformat(
                    str(data.get("recorded_at", datetime.now(UTC).isoformat()))
                ),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.debug(
                "JSON parse failed for %s: %s — treating as raw", filename, exc
            )
            return self._parse_raw(content, filename)

    def _parse_raw(self, content: str, filename: str) -> ModelFrictionEventLocal:
        """Create a synthetic friction event from raw file content."""
        first_line = content.strip().splitlines()[0] if content.strip() else filename
        return ModelFrictionEventLocal(
            event_id=str(uuid.uuid4()),
            friction_type="raw_file",
            description=first_line[:200],
            recorded_at=datetime.now(UTC),
        )


__all__: list[str] = ["AdapterFrictionReader"]
