"""Typed input and output of the green PR draft clock (OMN-19998).

Frozen dataclasses, not pydantic: the throughput tick calls the handler from the offline suites, where only the standard
library is installed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ModelGreenDraftClockRequest:
    previous: Mapping[str, Any] | None
    drafts_now: Mapping[str, bool]
    now: datetime


@dataclass(frozen=True)
class ModelGreenDraftClockResult:
    drafts: dict[str, str] = field(default_factory=dict)
    ready_since: dict[str, datetime] = field(default_factory=dict)
