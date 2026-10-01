# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed WithinBudgetParams contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelWithinBudgetParams(BaseModel):
    """Turn and call limits for every engine; wall time per engine (OMN-20233).

    ``max_wall_time_ms`` applies to an engine absent from
    ``max_wall_time_ms_by_engine``, which is keyed by the model id the run
    recorded (``ModelToolUseTranscript.engine``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_wall_time_ms: int = Field(ge=1)
    max_wall_time_ms_by_engine: dict[str, int] = Field(default_factory=dict)

    def wall_time_limit_ms(self, engine: str | None) -> int:
        """The wall-time limit of one engine, or the default when it has none."""
        if engine is None:
            return self.max_wall_time_ms
        return self.max_wall_time_ms_by_engine.get(engine, self.max_wall_time_ms)
