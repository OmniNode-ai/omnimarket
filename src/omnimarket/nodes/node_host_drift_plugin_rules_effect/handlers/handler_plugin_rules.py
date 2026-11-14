# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the host-drift plugin sync rules from their overlay (OMN-20689).

The overlay is the operator's plugin sync policy: a schema string and a list of rules, each
validating as ModelPluginSyncRule. A SAFE rule names an action; an ALERT_ONLY rule names none.
Any failure is REFUSED with a short detail; the handler never raises.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_plugin_revision import (
    ModelPluginSyncRule,
)
from omnimarket.models.model_plugin_rules import (
    EnumPluginRulesOutcome,
    ModelPluginRulesRequest,
    ModelPluginRulesResult,
)

OVERLAY_PATH = Path(__file__).resolve().parents[1] / "plugin_sync_policy.yaml"
EXPECTED_SCHEMA = "host-drift-plugin-sync-rules.v1"


class HandlerHostDriftPluginRules:
    """Effect: read the plugin sync rules overlay and validate its rules."""

    def __init__(self, overlay_path: Path | None = None) -> None:
        self._overlay_path = overlay_path if overlay_path is not None else OVERLAY_PATH

    def handle(self, request: ModelPluginRulesRequest) -> ModelPluginRulesResult:
        try:
            raw = yaml.safe_load(self._overlay_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return ModelPluginRulesResult(
                correlation_id=request.correlation_id,
                outcome=EnumPluginRulesOutcome.REFUSED,
                detail=f"cannot read overlay: {exc}",
            )
        if not isinstance(raw, dict) or raw.get("schema") != EXPECTED_SCHEMA:
            return ModelPluginRulesResult(
                correlation_id=request.correlation_id,
                outcome=EnumPluginRulesOutcome.REFUSED,
                detail=f"schema is not {EXPECTED_SCHEMA}",
            )
        items = raw.get("rules")
        if not isinstance(items, list):
            return ModelPluginRulesResult(
                correlation_id=request.correlation_id,
                outcome=EnumPluginRulesOutcome.REFUSED,
                detail="rules is not a list",
            )
        rules: list[ModelPluginSyncRule] = []
        for item in items:
            try:
                rule = ModelPluginSyncRule.model_validate(item)
            except ValidationError as exc:
                return ModelPluginRulesResult(
                    correlation_id=request.correlation_id,
                    outcome=EnumPluginRulesOutcome.REFUSED,
                    detail=f"rule fails validation: {exc.errors()[0]['msg']}",
                )
            if rule.sync_class == "SAFE" and not rule.action:
                return ModelPluginRulesResult(
                    correlation_id=request.correlation_id,
                    outcome=EnumPluginRulesOutcome.REFUSED,
                    detail=f"SAFE rule {rule.match} names no action",
                )
            if rule.sync_class == "ALERT_ONLY" and rule.action:
                return ModelPluginRulesResult(
                    correlation_id=request.correlation_id,
                    outcome=EnumPluginRulesOutcome.REFUSED,
                    detail=f"ALERT_ONLY rule {rule.match} names an action",
                )
            rules.append(rule)
        return ModelPluginRulesResult(
            correlation_id=request.correlation_id,
            outcome=EnumPluginRulesOutcome.LOADED,
            rules=tuple(rules),
        )
