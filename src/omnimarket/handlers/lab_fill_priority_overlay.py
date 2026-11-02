# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Loader of the ``lab_fill_priority`` and ``lab_fill_rank`` overlays (OMN-21904).

The caller loads both once and hands the validated models to the rank rule; a rule never reads
an overlay. A missing or invalid overlay raises :class:`NodeOverlayError` naming the overlay and
the fields, never a value. A failed load turns the ranked source off for the tick: there is no
fallback to default weights and no packaged default for the mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_fill.model_lab_fill_priority import (
    ModelLabFillPriorityDeclaration,
    ModelLabFillRankConfig,
)

PRIORITY_OVERLAY_ID = "lab_fill_priority"
RANK_OVERLAY_ID = "lab_fill_rank"


def load_rank_config(
    environ: Mapping[str, str] | None = None,
) -> ModelLabFillRankConfig:
    """The engineering-owned rank configuration; refuses naming the missing key."""
    return load_structured_overlay(RANK_OVERLAY_ID, ModelLabFillRankConfig, environ)


def load_priority_declaration(
    today: date,
    environ: Mapping[str, str] | None = None,
    config: ModelLabFillRankConfig | None = None,
) -> ModelLabFillPriorityDeclaration:
    """The operator's declaration, checked against the rank configuration's bounds.

    A boost whose ``expires`` is before ``today`` loads and is ignored (count it with
    ``boosts_expired``); one further than ``max_boost_days`` ahead, or with a multiplier outside
    the configured bounds, fails the load.
    """
    rank = config if config is not None else load_rank_config(environ)
    declaration = load_structured_overlay(
        PRIORITY_OVERLAY_ID, ModelLabFillPriorityDeclaration, environ
    )
    problems = declaration.field_problems(rank, today)
    if problems:
        raise NodeOverlayError(
            f"{PRIORITY_OVERLAY_ID} is invalid against {RANK_OVERLAY_ID} "
            f"(ValidationError): fields {sorted(problems)}"
        )
    return declaration
