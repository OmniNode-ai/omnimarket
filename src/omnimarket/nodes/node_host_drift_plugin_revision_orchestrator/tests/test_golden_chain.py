# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the host-drift plugin revision check (OMN-20689).

The golden chain: the packaged overlay loads, the rules route the probe facts through the compute,
and the verdict carries the sync action the policy names. The error chain: a refused rules load
leaves the item alert-only (no verdict). The failing control: a rules effect that hands over no
rules routes the same facts to alert-only, proving the verdict depends on the rules handed over.
"""

from __future__ import annotations

from omnimarket.models.model_plugin_revision import (
    EnumPluginDriftVerdict,
    ModelPluginProbeFacts,
)
from omnimarket.models.model_plugin_rules import (
    EnumPluginRulesOutcome,
    ModelPluginRulesResult,
)
from omnimarket.nodes.node_host_drift_plugin_revision_orchestrator.handlers.handler_plugin_revision_check import (
    HandlerHostDriftPluginRevisionCheck,
)
from omnimarket.nodes.node_host_drift_plugin_revision_orchestrator.models.model_plugin_revision_check import (
    EnumPluginRevisionCheckOutcome,
)

NAME = "onex-overlays@omninode-internal"


def overlays_fixture() -> ModelPluginProbeFacts:
    return ModelPluginProbeFacts(
        name=NAME,
        ref="main",
        installed_versions=("0.1.30",),
        merged_version="0.1.30",
        installed_revision="a" * 40,
        merged_tip="b" * 40,
        tip_in_installed=False,
    )


class StubRulesRefused:
    def handle(self, request: object) -> ModelPluginRulesResult:
        return ModelPluginRulesResult(
            correlation_id="stub",
            outcome=EnumPluginRulesOutcome.REFUSED,
            detail="stub refused",
        )


class StubRulesEmpty:
    def handle(self, request: object) -> ModelPluginRulesResult:
        return ModelPluginRulesResult(
            correlation_id="stub",
            outcome=EnumPluginRulesOutcome.LOADED,
            rules=(),
        )


def test_golden_the_packaged_overlay_routes_behind_to_install_plugin_tree() -> None:
    result = HandlerHostDriftPluginRevisionCheck().handle(overlays_fixture())
    assert result.outcome == EnumPluginRevisionCheckOutcome.CHECKED
    assert result.verdict is not None
    assert result.verdict.verdict == EnumPluginDriftVerdict.DRIFT
    assert result.verdict.behind is True
    assert result.verdict.sync_action == "install-plugin-tree"


def test_golden_omni_tree_modified_routes_between_lanes() -> None:
    facts = ModelPluginProbeFacts(
        name="omni@omninode-internal",
        ref="main",
        installed_versions=("0.1.30",),
        merged_version="0.1.30",
        installed_revision="a" * 40,
        merged_tip="b" * 40,
        tip_in_installed=False,
        tree_drift="tree modified",
    )
    result = HandlerHostDriftPluginRevisionCheck().handle(facts)
    assert result.outcome == EnumPluginRevisionCheckOutcome.CHECKED
    assert result.verdict is not None
    assert result.verdict.sync_action == "repair-plugin"
    assert result.verdict.between_lanes is True


def test_error_a_refused_rules_load_yields_rules_refused_with_no_verdict() -> None:
    result = HandlerHostDriftPluginRevisionCheck(
        rules_effect=StubRulesRefused()
    ).handle(overlays_fixture())
    assert result.outcome == EnumPluginRevisionCheckOutcome.RULES_REFUSED
    assert result.verdict is None
    assert "stub refused" in result.detail


def test_golden_omni_version_bump_routes_to_update_plugin() -> None:
    facts = ModelPluginProbeFacts(
        name="omni@omninode-internal",
        ref="main",
        installed_versions=("0.4.475",),
        merged_version="0.4.476",
        installed_revision="a" * 40,
        merged_tip="b" * 40,
        tip_in_installed=False,
    )
    result = HandlerHostDriftPluginRevisionCheck().handle(facts)
    assert result.outcome == EnumPluginRevisionCheckOutcome.CHECKED
    assert result.verdict is not None
    assert result.verdict.sync_action == "update-plugin"


def test_failing_control_omni_same_version_routes_to_install_plugin_tree() -> None:
    facts = ModelPluginProbeFacts(
        name="omni@omninode-internal",
        ref="main",
        installed_versions=("0.4.475",),
        merged_version="0.4.475",
        installed_revision="a" * 40,
        merged_tip="b" * 40,
        tip_in_installed=False,
    )
    result = HandlerHostDriftPluginRevisionCheck().handle(facts)
    assert result.outcome == EnumPluginRevisionCheckOutcome.CHECKED
    assert result.verdict is not None
    assert result.verdict.sync_action == "install-plugin-tree"


def test_failing_control_rules_not_handed_over_yields_no_sync_action() -> None:
    result = HandlerHostDriftPluginRevisionCheck(rules_effect=StubRulesEmpty()).handle(
        overlays_fixture()
    )
    assert result.outcome == EnumPluginRevisionCheckOutcome.CHECKED
    assert result.verdict is not None
    assert result.verdict.sync_action is None
