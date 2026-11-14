# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The display overlay of ``node_eval_board_compute`` (OMN-24174, plan task T8).

No key has a default: the first values are the plan's and a deployment sets them. A label or a
definition that carries an id-shaped token is refused at load, so no id can reach the tab
through the overlay.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)

ID_SHAPED = re.compile(
    r"\bOMN-\d+\b"
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-fA-F]{12,}\b"
    r"|\bnode_[A-Za-z0-9_]+"
    r"|\bonex\.[A-Za-z0-9_.\-]+"
)
"""An id-shaped token: a ticket id, a uuid, a hex digest of 12 or more characters, ``node_`` or ``onex.``."""

GLOSSARY_TERMS: tuple[str, ...] = ("task", "pass", "size", "likely range")
"""The four words the tab defines, in the order it prints them."""


class ModelEvalBoardOverlay(BaseModel):
    """The ``evals.board.*`` keys the board reads, and the routing line its size cells use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_names: dict[str, str] = Field(
        min_length=1,
        description="evals.board.display_names: model id to plain label, in the order the tab lists models.",
    )
    max_models: int = Field(ge=1, description="evals.board.max_models")
    min_n_point: int = Field(
        ge=1,
        description="evals.board.min_n_point: a rate from fewer tasks than this is never drawn as a number.",
    )
    trend_look_points: int = Field(
        ge=1,
        description="evals.board.trend_look_points: the change in percentage points worth a look.",
    )
    glossary: dict[str, str] = Field(
        description="evals.board.glossary: one definition for each of task, pass, size and likely range."
    )
    size_line: ModelCapabilityLine = Field(
        description="The routing line a size cell is judged against, the capability map's own line."
    )

    @field_validator("display_names")
    @classmethod
    def _labels_are_plain(cls, value: dict[str, str]) -> dict[str, str]:
        for label in value.values():
            if not label.strip() or ID_SHAPED.search(label):
                raise ValueError(
                    "a display name is empty or carries an id-shaped token"
                )
        return value

    @field_validator("glossary")
    @classmethod
    def _glossary_is_complete(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(GLOSSARY_TERMS):
            raise ValueError(
                f"the glossary defines exactly {', '.join(GLOSSARY_TERMS)}"
            )
        for definition in value.values():
            if not definition.strip() or ID_SHAPED.search(definition):
                raise ValueError("a definition is empty or carries an id-shaped token")
        return value


__all__ = ["GLOSSARY_TERMS", "ID_SHAPED", "ModelEvalBoardOverlay"]
