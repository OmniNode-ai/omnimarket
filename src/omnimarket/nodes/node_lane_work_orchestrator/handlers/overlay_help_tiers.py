# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the help chain from the orchestrator's overlay (OMN-26722).

The overlay resolver's ordered roots decide where the chain comes from: the settings store's root first, then the
declared overlay file (bootstrap). The first ``node_lane_work_orchestrator/overlay.yaml`` found is the document; a
document with no chain, or no document at all, is a refusal that names both places. There is no built-in chain.
"""

from __future__ import annotations

from collections.abc import Mapping

import yaml
from omnibase_core.models.lane_work import ModelLaneHelpTier
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.handlers.node_overlay_resolved_roots import resolved_overlay_environ
from omnimarket.models.overlay_documents.model_lane_work_orchestrator_overlay import (
    ModelLaneWorkOrchestratorOverlay,
)

NODE_NAME = "node_lane_work_orchestrator"
HELP_TIERS_KEY = "help_tiers"


class HelpTiersError(ValueError):
    """No usable help chain in the settings store or the overlay file."""


def _refusal(reason: str) -> HelpTiersError:
    return HelpTiersError(
        f"{reason}: set {HELP_TIERS_KEY} in the settings store's {NODE_NAME}/overlay.yaml "
        f"(node_overlay_setting_set_effect) or, for bootstrap only, in the declared overlay file "
        f"{NODE_NAME}/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS"
    )


def load_help_tiers(
    environ: Mapping[str, str] | None = None,
) -> tuple[ModelLaneHelpTier, ...]:
    """The chain from the first resolver root that holds the orchestrator's overlay (the store's root first)."""
    found = find_node_overlay(NODE_NAME, resolved_overlay_environ(environ))
    if found is None:
        raise _refusal("no orchestrator overlay in either place")
    loaded = yaml.safe_load(found.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not loaded.get(HELP_TIERS_KEY):
        raise _refusal(f"the overlay at {found} holds no {HELP_TIERS_KEY}")
    try:
        document = ModelLaneWorkOrchestratorOverlay.model_validate(loaded)
    except ValidationError as error:
        raise _refusal(
            f"the overlay at {found} holds an invalid chain ({error})"
        ) from error
    return document.help_tiers


__all__ = ["HELP_TIERS_KEY", "NODE_NAME", "HelpTiersError", "load_help_tiers"]
