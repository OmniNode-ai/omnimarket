# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PlaybookRegistry: loads alert_remediation_playbooks.yaml and matches events.

Usage::

    registry = PlaybookRegistry.load()
    match = registry.match(event)
    if match:
        prompt = match.render(event)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_event import (
        ModelAlertEvent,
    )

logger = logging.getLogger(__name__)

_PLAYBOOKS_PATH = Path(__file__).parent / "alert_remediation_playbooks.yaml"


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    pattern_keywords: tuple[str, ...]
    severity_scope: tuple[str, ...]
    dispatch_prompt: str
    dod_evidence: tuple[str, ...]

    def matches(self, event: ModelAlertEvent) -> bool:
        pattern_lower = event.pattern_matched.lower()
        return any(kw.lower() in pattern_lower for kw in self.pattern_keywords)

    def render(self, event: ModelAlertEvent) -> str:
        return self.dispatch_prompt.format(
            container=event.container,
            host=event.host,
            detected_at=event.detected_at,
            pattern_matched=event.pattern_matched,
            restart_count=event.restart_count
            if event.restart_count is not None
            else "N/A",
            source=event.source,
            severity=event.severity,
            alert_id=event.alert_id,
        )


@dataclass
class PlaybookRegistry:
    playbooks: tuple[Playbook, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: Path | None = None) -> PlaybookRegistry:
        """Load playbooks from YAML. Falls back to empty registry on error."""
        target = path or _PLAYBOOKS_PATH
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
            entries = data.get("playbooks", [])
            playbooks = tuple(
                Playbook(
                    id=p["id"],
                    title=p["title"],
                    pattern_keywords=tuple(p.get("pattern_keywords", [])),
                    severity_scope=tuple(p.get("severity_scope", [])),
                    dispatch_prompt=p.get("dispatch_prompt", ""),
                    dod_evidence=tuple(p.get("dod_evidence", [])),
                )
                for p in entries
            )
            logger.debug(
                "PlaybookRegistry: loaded %d playbooks from %s", len(playbooks), target
            )
            return cls(playbooks=playbooks)
        except Exception as exc:
            logger.warning("PlaybookRegistry: failed to load %s: %s", target, exc)
            return cls(playbooks=())

    def match(self, event: ModelAlertEvent) -> Playbook | None:
        """Return first playbook whose keywords match the event pattern, or None."""
        for pb in self.playbooks:
            if pb.matches(event):
                return pb
        return None

    def __len__(self) -> int:
        return len(self.playbooks)
