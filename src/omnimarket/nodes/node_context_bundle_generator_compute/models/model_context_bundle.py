"""Context bundle model — progressive L0-L4 context structure."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumContextLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class ModelContextBundleL0(BaseModel):
    """Minimal identity — ticket ID only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    level: EnumContextLevel = EnumContextLevel.L0


class ModelContextBundleL1(ModelContextBundleL0):
    """L0 + task state (status, assignee, priority, title)."""

    title: str = ""
    status: str = ""
    assignee: str = ""
    priority: str = ""
    labels: tuple[str, ...] = ()
    level: EnumContextLevel = EnumContextLevel.L1


class ModelContextBundleL2(ModelContextBundleL1):
    """L1 + run context (session, agent, timing)."""

    session_id: str = ""
    agent_id: str = ""
    timestamp: str = ""
    worker_type: str = ""
    repo: str = ""
    branch: str = ""
    trigger_event: str = ""
    level: EnumContextLevel = EnumContextLevel.L2


class ModelContextBundleL3(ModelContextBundleL2):
    """L2 + relationships (parent, related tickets)."""

    parent_ticket_id: str = ""
    related_ticket_ids: tuple[str, ...] = ()
    level: EnumContextLevel = EnumContextLevel.L3


class ModelContextBundleL4(ModelContextBundleL3):
    """L3 + historical annotation slot (populated by callers with prior run data)."""

    historical_summary: str = ""
    prior_attempt_count: int = 0
    level: EnumContextLevel = EnumContextLevel.L4


__all__ = [
    "EnumContextLevel",
    "ModelContextBundleL0",
    "ModelContextBundleL1",
    "ModelContextBundleL2",
    "ModelContextBundleL3",
    "ModelContextBundleL4",
]
