# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model overlay write effect is asked to write (OMN-20330)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The three developer modes: native ``onex``, the local compose stack, the lab stack.
OverlayMode = Literal["native", "local_stack", "lab_stack"]


class ModelModelOverlayWriteRequest(BaseModel):
    """Write the model overlay for ``mode``; no key is ever part of a request.

    ``source="ollama"``: the Ollama default for ``memory_gb``, or the row of the
    contract's ``ollama`` block that names ``served_model_id`` (the block states
    model, window and ceiling). ``source="url"``: the
    server at ``url`` (a base URL; the chat path is appended) serving
    ``served_model_id``, with ``parameter_count`` and ``context_window`` stated.
    ``max_tokens`` and ``timeout_ms`` fall back to this node's contract config.
    ``target_path`` replaces the mode's default file (tests, a custom home).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: OverlayMode
    source: Literal["ollama", "url"]
    memory_gb: int | None = Field(default=None, ge=0)
    url: str | None = None
    served_model_id: str | None = None
    parameter_count: str | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_ms: int | None = Field(default=None, gt=0)
    target_path: Path | None = None
    #: The caller's own first line for the file, when it recognises its files by it.
    marker_line: str | None = Field(default=None, pattern=r"^#")

    @model_validator(mode="after")
    def _source_fields(self) -> Self:
        if self.source == "ollama" and self.memory_gb is None:
            raise ValueError("source 'ollama' needs memory_gb")
        if self.source == "url":
            missing = [
                name
                for name in (
                    "url",
                    "served_model_id",
                    "parameter_count",
                    "context_window",
                )
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"source 'url' needs {', '.join(missing)}")
        return self


__all__ = ["ModelModelOverlayWriteRequest", "OverlayMode"]
