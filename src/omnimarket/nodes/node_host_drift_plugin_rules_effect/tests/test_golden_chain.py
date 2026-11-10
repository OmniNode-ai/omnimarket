# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the plugin sync rules overlay (OMN-20689)."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.events.plugin_rules import (
    EnumPluginRulesOutcome as Outcome,
)
from omnimarket.events.plugin_rules import (
    ModelPluginRulesRequest,
)
from omnimarket.nodes.node_host_drift_plugin_rules_effect.handlers.handler_plugin_rules import (
    HandlerHostDriftPluginRules,
)


def test_golden_the_packaged_overlay_loads_and_keeps_rule_order() -> None:
    result = HandlerHostDriftPluginRules().handle(
        ModelPluginRulesRequest(correlation_id="golden")
    )
    assert result.outcome == Outcome.LOADED, result.detail
    assert len(result.rules) >= 2
    first = result.rules[0]
    assert first.match == "plugin:omni@omninode-internal"
    assert "tree modified" in first.observed_contains
    assert first.action == "repair-plugin"
    assert first.between_lanes is True
    assert first.sync_class == "SAFE"


def test_error_a_wrong_schema_string_is_refused(tmp_path: Path) -> None:
    overlay = tmp_path / "policy.yaml"
    overlay.write_text(yaml.safe_dump({"schema": "wrong.v1", "rules": []}))
    result = HandlerHostDriftPluginRules(overlay_path=overlay).handle(
        ModelPluginRulesRequest(correlation_id="bad-schema")
    )
    assert result.outcome == Outcome.REFUSED
    assert "schema" in result.detail


def test_error_a_safe_rule_with_no_action_is_refused(tmp_path: Path) -> None:
    overlay = tmp_path / "policy.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema": "host-drift-plugin-sync-rules.v1",
                "rules": [
                    {
                        "match": "plugin:x",
                        "kinds": ["plugin"],
                        "sync_class": "SAFE",
                    }
                ],
            }
        )
    )
    result = HandlerHostDriftPluginRules(overlay_path=overlay).handle(
        ModelPluginRulesRequest(correlation_id="no-action")
    )
    assert result.outcome == Outcome.REFUSED
    assert "action" in result.detail


def test_error_a_missing_file_is_refused(tmp_path: Path) -> None:
    result = HandlerHostDriftPluginRules(overlay_path=tmp_path / "nope.yaml").handle(
        ModelPluginRulesRequest(correlation_id="missing")
    )
    assert result.outcome == Outcome.REFUSED
    assert "read" in result.detail


def test_failing_control_swapped_policy_changes_the_first_rule(tmp_path: Path) -> None:
    """The injected break `swapped_policy`: a handler pointed at a file whose first rule is swapped."""
    overlay = tmp_path / "swapped.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema": "host-drift-plugin-sync-rules.v1",
                "rules": [
                    {
                        "match": "plugin:swapped",
                        "kinds": ["plugin"],
                        "sync_class": "SAFE",
                        "action": "swapped-action",
                    }
                ],
            }
        )
    )
    result = HandlerHostDriftPluginRules(overlay_path=overlay).handle(
        ModelPluginRulesRequest(correlation_id="swapped_policy")
    )
    assert result.outcome == Outcome.LOADED
    assert result.rules[0].match == "plugin:swapped"
    assert result.rules[0].action == "swapped-action"
