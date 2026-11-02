# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adapter contract's client_bindings.claude_code section (OMN-18531)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    ModelClientBinding,
)


class ModelClientSection(BaseModel):
    """Per-host maximum modes and the binding rows; the inventory of binding hooks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_max_mode: EnumClientMaxMode = Field(default=EnumClientMaxMode.OBSERVE)
    host_max_modes: dict[str, EnumClientMaxMode] = Field(default_factory=dict)
    bindings: tuple[ModelClientBinding, ...] = Field(default=())

    @model_validator(mode="before")
    @classmethod
    def _render_awaited_event_parts(cls, data: Any) -> Any:
        """Render each row's `awaited_event_parts` into `awaited_events` (OMN-22103).

        A part row renders to `onex.<kind>.<producer>.<name_prefix>{principal}.v<version>`;
        the contract declares parts because a `{principal}` slot inside a full topic string
        fails the topic-name lint.
        """
        if not isinstance(data, dict):
            return data
        rows = []
        for row in data.get("bindings") or ():
            if isinstance(row, dict) and "awaited_event_parts" in row:
                row = {k: v for k, v in row.items() if k != "awaited_event_parts"} | {
                    "awaited_events": [
                        f"onex.{p['kind']}.{p['producer']}.{p['name_prefix']}"
                        f"{{principal}}.v{p['version']}"
                        for p in row["awaited_event_parts"]
                    ]
                }
            rows.append(row)
        return data | {"bindings": rows} if "bindings" in data else data
