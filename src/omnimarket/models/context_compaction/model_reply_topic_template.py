# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One per-principal reply topic, declared in parts (OMN-22103).

The orchestrator contract declares each reply topic as its parts, the way the
work-ledger writer topics are declared, because a ``{principal}`` slot inside a
full topic string fails the topic-name lint. The parts render to
``onex.<kind>.<producer>.<name_prefix><principal>.v<version>``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class ModelReplyTopicTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    name_prefix: str = Field(min_length=1)
    version: int = Field(ge=1)
    principal_pattern: str = Field(min_length=1)

    @property
    def template(self) -> str:
        """The topic with its one ``{principal}`` slot."""
        return (
            f"onex.{self.kind}.{self.producer}.{self.name_prefix}{{principal}}"
            f".v{self.version}"
        )

    def topic_for(self, principal: str) -> str:
        """The topic of ``principal``; a principal outside the declared pattern is refused."""
        if re.fullmatch(self.principal_pattern, principal) is None:
            raise ValueError("principal does not match the declared principal pattern")
        return self.template.replace("{principal}", principal)

    def principal_of(self, topic: str) -> str | None:
        """The principal ``topic`` is for, or None when it is not of this template."""
        bare = self.principal_pattern.removeprefix("^").removesuffix("$")
        head, tail = self.template.split("{principal}")
        match = re.fullmatch(f"{re.escape(head)}({bare}){re.escape(tail)}", topic)
        return match.group(1) if match is not None else None


__all__ = ["ModelReplyTopicTemplate"]
