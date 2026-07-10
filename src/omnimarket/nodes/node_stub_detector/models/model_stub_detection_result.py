"""Result model: the set of method stubs detected in a node source file."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_stub_detector.models.model_stub import ModelStub


class ModelStubDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stubs: tuple[ModelStub, ...]


__all__ = ["ModelStubDetectionResult"]
