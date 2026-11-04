# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-side overlay assembly for the exception effect."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.error_rule_match import ModelErrorRuleNoMatchOverlay


def load_error_rule_nomatch_overlay(
    environ: Mapping[str, str] | None = None, *, explicit: object | None = None
) -> ModelErrorRuleNoMatchOverlay:
    return load_structured_overlay(
        "node_error_rule_nomatch_effect",
        ModelErrorRuleNoMatchOverlay,
        environ,
        explicit=explicit,
    )
