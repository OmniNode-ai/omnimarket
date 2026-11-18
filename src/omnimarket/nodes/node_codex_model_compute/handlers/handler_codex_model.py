# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the Codex model from the overlay policy and the lane's routing facts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models.model_codex_model import (
    ModelCodexModelDeclaration,
    ModelCodexModelRequest,
    ModelCodexModelResult,
)

NODE = "node_codex_model_compute"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_declaration(
    environ: Mapping[str, str] | None = None,
) -> ModelCodexModelDeclaration:
    """Load and validate the declared model ids and their routing policy."""
    return load_structured_overlay(NODE, ModelCodexModelDeclaration, environ)


class HandlerCodexModel:
    def __init__(self, declaration: ModelCodexModelDeclaration | None = None) -> None:
        self.declaration = load_declaration() if declaration is None else declaration

    def handle(self, request: ModelCodexModelRequest) -> ModelCodexModelResult:
        declared = self.declaration.codex_models
        asked = (request.codex_model or "").strip()
        if asked:
            if request.codex_model not in declared:
                return ModelCodexModelResult(
                    accepted=False,
                    reason=f"codex model {asked!r} is not declared; the declared ids are "
                    + ", ".join(declared),
                    declared=declared,
                )
            model, reason = request.codex_model, f"declared id {request.codex_model!r}"
        elif request.security_scope:
            model, reason = self.declaration.escalation_model, "security scope"
        elif request.round_count >= self.declaration.escalation_round_threshold:
            model, reason = (
                self.declaration.escalation_model,
                "escalation round reached",
            )
        else:
            model, reason = self.declaration.initial_model, "overlay default"
        return ModelCodexModelResult(
            accepted=True,
            model=model,
            reason=reason,
            declared=declared,
        )
