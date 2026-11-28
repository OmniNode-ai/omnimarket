# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read of the owner's file at the owner's base and the objects the ask would name."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCrossOwnerAskRequest(BaseModel):
    """``read_command`` is what the caller ran and ``observed_text`` is its output.

    ``names`` are the objects the ask would have the owner add, bare or qualified
    (``public.some_seq``). A file that declares an object by its bare name under a
    domain holds it for every schema, so every name is also looked up by its bare part.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    base: str = Field(min_length=1)
    path: str = Field(min_length=1)
    names: tuple[str, ...] = Field(min_length=1)
    read_command: str
    read_exit_code: int
    observed_text: str
