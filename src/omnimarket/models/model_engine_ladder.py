# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine ladder as the ``config.engine_ladder`` field of ``node_remote_lane_compute`` (OMN-21475).

Rungs run from the cheapest to the dearest. Each entry pairs a rung id (``sonnet``) with the
host-engine id a placement request carries (``claude_sonnet``). The ladder is data of the
contract; a model that validates against it takes it from the validation context
(``context={"engine_ladder": ladder}``) and otherwise from the packaged contract.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

CONTEXT_KEY = "engine_ladder"


class ModelEngineLadderEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: str = Field(min_length=1)
    host_engine: str = Field(min_length=1)


class ModelEngineLadder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelEngineLadderEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_rungs(self) -> Self:
        rungs = [entry.rung for entry in self.entries]
        if len(set(rungs)) != len(rungs):
            raise ValueError("engine_ladder rungs must be distinct")
        return self

    @property
    def rungs(self) -> tuple[str, ...]:
        return tuple(entry.rung for entry in self.entries)

    @property
    def host_engines(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.host_engine for entry in self.entries))

    def index(self, rung: str) -> int:
        return self.rungs.index(rung)

    def host_engine(self, rung: str) -> str:
        return self.entries[self.index(rung)].host_engine


def ladder_of(info: ValidationInfo) -> ModelEngineLadder:
    """The ladder a validator checks against: the validation context's, else the packaged contract's."""
    context = info.context
    if isinstance(context, dict) and CONTEXT_KEY in context:
        ladder = context[CONTEXT_KEY]
        if not isinstance(ladder, ModelEngineLadder):
            raise TypeError(f"{CONTEXT_KEY} must be a ModelEngineLadder")
        return ladder
    from omnimarket.handlers.handler_engine_ladder import load_engine_ladder

    return load_engine_ladder()
