# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction command the adapter publishes (OMN-21113, TA.11).

It carries no conversation content: every source the package needs is read
behind the bus. ``invocation_id`` is minted by the adapter for every invocation
and is echoed on the package reply and on any fallback event.

The command names the three per-principal topics the reply is published to
(OMN-22103). Each name must be of the shape its template in the orchestrator
contract declares, and all three must be for one principal, so a requester can
only direct a reply to a topic of the declared pattern, never to an arbitrary or
another family's topic.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from omnimarket.models.context_compaction.enum_context_compaction_trigger import (
    EnumContextCompactionTrigger,
)
from omnimarket.models.context_compaction.model_reply_topic_templates import (
    load_reply_topic_templates,
)


class ModelContextCompactionRequested(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    session_id: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    trigger: EnumContextCompactionTrigger
    input_messages_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    newest_turn: str | None = None
    compact_model: str | None = Field(default=None, min_length=1)
    #: The session barrier the adapter journalled before publishing; the
    #: orchestrator waits on it before it reads any source.
    barrier_id: str | None = Field(default=None, min_length=1)
    #: The per-principal topics the orchestrator replies to (OMN-22103).
    reply_topic_package: str
    reply_topic_prepared: str
    reply_topic_fallback: str

    @field_validator(
        "reply_topic_package", "reply_topic_prepared", "reply_topic_fallback"
    )
    @classmethod
    def _of_the_declared_template(cls, value: str, info: ValidationInfo) -> str:
        templates = load_reply_topic_templates()
        template = getattr(templates, str(info.field_name).removeprefix("reply_topic_"))
        if template.principal_of(value) is None:
            raise ValueError(
                f"{info.field_name} is not a topic of the declared pattern {template.template}"
            )
        return value

    @model_validator(mode="after")
    def _one_principal(self) -> Self:
        templates = load_reply_topic_templates()
        principals = {
            templates.package.principal_of(self.reply_topic_package),
            templates.prepared.principal_of(self.reply_topic_prepared),
            templates.fallback.principal_of(self.reply_topic_fallback),
        }
        if len(principals) != 1:
            raise ValueError("the three reply topics must be for one principal")
        return self


__all__ = ["ModelContextCompactionRequested"]
