# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A contract's ``writer_topics`` template: one command topic per writer principal.

The one derivation of a writer's topic name. The work ledger serve (``work_ledger_bus.bus``) and the
topic provision plan both compose the name here, so the topic the serve subscribes to and the topic
provisioning creates cannot differ.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class ModelWriterTopicsTemplate(BaseModel):
    """``onex.<kind>.<producer>.<name_prefix><principal>.v<version>`` for each admitted principal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    name_prefix: str = Field(min_length=1)
    version: int = Field(ge=1)
    principal_pattern: str = Field(min_length=1)

    def topic_template(self) -> str:
        """The topic name with a ``{principal}`` slot where the writer's name goes."""
        return (
            f"onex.{self.kind}.{self.producer}."
            f"{self.name_prefix}{{principal}}.v{self.version}"
        )

    def admits(self, principal: str) -> bool:
        """Whether the principal passes the template's pattern."""
        return re.fullmatch(self.principal_pattern, principal) is not None

    def topic_for(self, principal: str) -> str:
        """The writer's command topic; raises ValueError for a principal the pattern refuses."""
        if not self.admits(principal):
            raise ValueError(f"invalid writer principal {principal!r}")
        return self.topic_template().format(principal=principal)
