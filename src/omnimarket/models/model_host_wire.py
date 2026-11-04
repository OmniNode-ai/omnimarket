# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared validation and canonical JSON transport for host state records."""

import json
from datetime import datetime
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
HostSurface = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
]


def _validate_utc_stamp(value: str) -> str:
    """Reject impossible dates as well as timestamps outside the UTC wire format."""
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


UtcStamp = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    AfterValidator(_validate_utc_stamp),
]


class ModelHostWire(BaseModel):
    """An immutable host record with deterministic UTF-8 JSON serialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_wire(self) -> bytes:
        """Serialize every field with canonical ordering and no extra whitespace."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_wire(cls, data: bytes) -> Self:
        """Parse and validate a JSON record from the wire."""
        return cls.model_validate_json(data)
