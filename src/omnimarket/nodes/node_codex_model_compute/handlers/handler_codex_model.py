# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which Codex model a lane runs: the account default unless a declared id is asked (OMN-25552).

A lane asks for no model and gets none (no ``-m``; the account's default model runs), asks for a
declared id and gets it, and asks for anything else and is refused with the declared ids named.
The declared ids are the node's overlay (``codex_models``); the node holds none itself.
"""

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
    """The overlay's declared ids; no overlay, or a broken one, refuses (there is no packaged fallback)."""
    return load_structured_overlay(NODE, ModelCodexModelDeclaration, environ)


class HandlerCodexModel:
    def __init__(self, declaration: ModelCodexModelDeclaration | None = None) -> None:
        self.declaration = load_declaration() if declaration is None else declaration

    def handle(self, request: ModelCodexModelRequest) -> ModelCodexModelResult:
        declared = self.declaration.codex_models
        asked = (request.codex_model or "").strip()
        if not asked:
            return ModelCodexModelResult(
                accepted=True,
                reason="no model asked: the account default runs (no -m)",
                declared=declared,
            )
        if request.codex_model in declared:
            return ModelCodexModelResult(
                accepted=True,
                model=request.codex_model,
                reason=f"declared id {request.codex_model!r}",
                declared=declared,
            )
        return ModelCodexModelResult(
            accepted=False,
            reason=f"codex model {asked!r} is not declared; the declared ids are "
            + ", ".join(declared),
            declared=declared,
        )
