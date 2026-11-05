# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load a core-shaped seed document and bind its versions at the caller."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.error_rule_match import (
    ModelErrorRuleKeyVersion,
    ModelErrorRuleTable,
    ModelErrorRuleTableDocument,
)


def load_error_rule_table(
    environ: Mapping[str, str] | None = None,
    *,
    key_versions: tuple[ModelErrorRuleKeyVersion, ...],
    explicit: object | None = None,
) -> ModelErrorRuleTable:
    """The caller obtains authority/closure validation from core before matching."""
    document = load_structured_overlay(
        "error_rules", ModelErrorRuleTableDocument, environ, explicit=explicit
    )
    return ModelErrorRuleTable(rows=document.error_rules, key_versions=key_versions)
