"""Typed input and output of the controller's not-green read for the green-unlanded alarm (OMN-19998).

Frozen dataclasses, not pydantic: the throughput tick calls the handler from the offline suites, where only the standard
library is installed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ModelGreenControllerReadRequest:
    facts: Mapping[str, Any] | None
    heads: Mapping[str, str]
    now: datetime | None = None
    bound_min: float | None = None


@dataclass(frozen=True)
class ModelGreenControllerReadResult:
    not_green: frozenset[str] = field(default_factory=frozenset)
