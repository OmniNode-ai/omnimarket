# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reply the adapter awaits, correlated by ``invocation_id`` (OMN-21113, TA.11).

``install`` carries the messages and their ``installed_messages_hash``;
``native`` carries neither, and the binding runs native compaction.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)

_COMPACTION_ACTIONS = frozenset(
    {EnumClientVerdictAction.INSTALL, EnumClientVerdictAction.NATIVE}
)


class ModelContextCompactionPackageReply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    action: EnumClientVerdictAction
    messages: tuple[ModelContextPackRenderMessage, ...] = ()
    installed_messages_hash: str | None = None

    @model_validator(mode="after")
    def _install_carries_a_package(self) -> Self:
        if self.action not in _COMPACTION_ACTIONS:
            raise ValueError(
                f"a compaction reply is install or native, not {self.action}"
            )
        installs = self.action is EnumClientVerdictAction.INSTALL
        if installs != bool(self.messages) or installs != (
            self.installed_messages_hash is not None
        ):
            raise ValueError(
                "install carries messages and their hash; native carries neither"
            )
        return self


__all__ = ["ModelContextCompactionPackageReply"]
