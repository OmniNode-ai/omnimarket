"""Result model: structured analysis of a single ONEX node source file."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelNodeAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str
    base_class: str
    mixins: tuple[str, ...]
    methods: tuple[str, ...]
    docstring: str | None
    io_operations: tuple[str, ...]


__all__ = ["ModelNodeAnalysis"]
