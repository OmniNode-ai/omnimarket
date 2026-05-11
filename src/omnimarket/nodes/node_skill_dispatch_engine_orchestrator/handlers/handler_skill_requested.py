# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill dispatch handler for node_skill_dispatch_engine_orchestrator (scaffold).

Scaffold only: live dispatch returns a ``"dispatched"`` placeholder. Real
dispatch wiring to the polymorphic agent is follow-up work.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["HandlerSkillRequested"]

logger = logging.getLogger(__name__)


class HandlerSkillRequested:
    """Thin handler shell for the dispatch_engine skill request event.

    Scaffold semantics:
        * ``dry_run=True`` → returns ``{"status": "dry_run", ...}`` without
          any side effects.
        * ``dry_run=False`` → returns ``{"status": "dispatched", ...}`` as a
          placeholder. Real dispatch wiring is follow-up.
    """

    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def handle_skill_requested(
        self,
        *,
        skill_name: str,
        skill_path: str,
        args: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Handle a skill-requested event.

        Returns a plain dict with at least ``status`` set to ``"dispatched"``
        or ``"dry_run"``. The return shape is intentionally simple for the
        scaffold; a richer typed ``ModelSkillResult`` is used once live
        dispatch is wired.
        """
        args = args or {}
        if not skill_name or not skill_name.strip():
            raise ValueError("skill_name must not be blank")
        if not skill_path or not skill_path.endswith("SKILL.md"):
            raise ValueError("skill_path must end with 'SKILL.md'")

        if dry_run:
            logger.debug(
                "dispatch_engine dry_run for skill=%r path=%r", skill_name, skill_path
            )
            return {
                "skill_name": skill_name,
                "skill_path": skill_path,
                "args": dict(args),
                "status": "dry_run",
            }

        # Scaffold: real dispatch wiring is follow-up.
        logger.debug(
            "dispatch_engine placeholder dispatch for skill=%r path=%r",
            skill_name,
            skill_path,
        )
        return {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "args": dict(args),
            "status": "dispatched",
        }
