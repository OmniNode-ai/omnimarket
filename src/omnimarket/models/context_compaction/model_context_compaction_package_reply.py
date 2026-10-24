# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reply the adapter awaits, correlated by ``invocation_id`` (OMN-21113, TA.11).

``install`` carries the messages and their ``installed_messages_hash``;
``native`` carries neither, and the binding runs native compaction.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self
from uuid import UUID

from omnibase_core.enums.enum_client_verdict_action import EnumClientVerdictAction
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)

_COMPACTION_ACTIONS = frozenset(
    {EnumClientVerdictAction.INSTALL, EnumClientVerdictAction.NATIVE}
)


def installed_messages_json(messages: tuple[ModelContextPackRenderMessage, ...]) -> str:
    """The canonical JSON of the role/text list: the text the adapter installs."""
    return json.dumps(
        [m.model_dump() for m in messages],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def installed_messages_hash(messages: tuple[ModelContextPackRenderMessage, ...]) -> str:
    """sha256 over the canonical JSON of the role/text list, as the renderer hashes a body."""
    return hashlib.sha256(installed_messages_json(messages).encode("utf-8")).hexdigest()


class ModelContextCompactionPackageReply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    action: EnumClientVerdictAction
    status: Literal["built", "native"]
    package_id: UUID | None = None
    messages: tuple[ModelContextPackRenderMessage, ...] = ()
    installed_messages_hash: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_action_status(cls, value: Any) -> Any:
        """Older adapters send action only; retain that input shape."""
        if isinstance(value, dict) and "status" not in value:
            return {
                **value,
                "status": "built" if value.get("action") == "install" else "native",
            }
        return value

    @model_validator(mode="after")
    def _install_carries_a_package(self) -> Self:
        if self.action not in _COMPACTION_ACTIONS:
            raise ValueError(
                f"a compaction reply is install or native, not {self.action}"
            )
        installs = self.action is EnumClientVerdictAction.INSTALL
        if installs != (self.status == "built"):
            raise ValueError("compaction status must agree with its action")
        if not installs and self.package_id is not None:
            raise ValueError("native compaction carries no package id")
        if installs != bool(self.messages) or installs != (
            self.installed_messages_hash is not None
        ):
            raise ValueError(
                "install carries messages and their hash; native carries neither"
            )
        return self


__all__ = [
    "ModelContextCompactionPackageReply",
    "installed_messages_hash",
    "installed_messages_json",
]
