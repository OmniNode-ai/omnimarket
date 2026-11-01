# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``andon_stop`` overlay of the stop reducer.

Shape only; the values (the mode of each class and the citation that moved it to
``enforce``) belong to the deployment owner. The caller loads the overlay once and
passes the resulting policy to ``stop_gate``; a handler never reads it. A class with
no mode makes ``stop_gate`` refuse naming ``andon_stop.modes.<class>``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.stop_gate import EnumStopMode, ModelStopPolicy

OVERLAY_ID = "andon_stop"


class ModelAndonStopOverlay(BaseModel):
    """``<root>/andon_stop/overlay.yaml``; every key is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    modes: dict[str, EnumStopMode]
    enforce_citations: dict[str, str]

    def policy(self) -> ModelStopPolicy:
        """The overlay as the gate's policy, refusing an enforced class with no citation."""
        for class_id, mode in self.modes.items():
            if (
                mode is EnumStopMode.ENFORCE
                and not self.enforce_citations.get(class_id, "").strip()
            ):
                raise NodeOverlayError(
                    f"{OVERLAY_ID}.enforce_citations.{class_id} is missing: a class "
                    f"in enforce mode needs the citation that moved it"
                )
        return ModelStopPolicy(
            modes=self.modes, enforce_citations=self.enforce_citations
        )


def load_stop_policy(environ: Mapping[str, str] | None = None) -> ModelStopPolicy:
    """Load and validate the ``andon_stop`` overlay; refuses naming the missing key."""
    return load_structured_overlay(OVERLAY_ID, ModelAndonStopOverlay, environ).policy()
