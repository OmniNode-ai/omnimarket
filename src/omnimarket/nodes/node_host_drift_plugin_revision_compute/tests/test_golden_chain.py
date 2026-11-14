# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests for the plugin revision decision (OMN-20689).

The failing control: onex-overlays at an UNCHANGED version 0.1.30 on an older revision must sync.
`version_only_verdict` is the old behaviour (a version compare) kept here as the named injected
break; it reads the same fixture as latest, and the revision decision reads it as drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_plugin_revision import (
    EnumPluginDriftVerdict,
    ModelPluginRevisionFacts,
    ModelPluginSyncRule,
)
from omnimarket.models.model_plugin_rules import (
    ModelPluginRulesRequest,
)
from omnimarket.nodes.node_host_drift_plugin_revision_compute.handlers import (
    handler_plugin_revision as handler_module,
)
from omnimarket.nodes.node_host_drift_plugin_revision_compute.handlers.handler_plugin_revision import (
    HandlerHostDriftPluginRevision,
    rule_for,
)
from omnimarket.nodes.node_host_drift_plugin_rules_effect.handlers.handler_plugin_rules import (
    HandlerHostDriftPluginRules,
)

OVERLAYS = "onex-overlays@omninode-internal"
OMNI = "omni@omninode-internal"
OLD, TIP = "a" * 40, "b" * 40


def overlay_rules() -> tuple[ModelPluginSyncRule, ...]:
    """The packaged overlay's rules, read by the rules effect and handed to the compute as input."""
    result = HandlerHostDriftPluginRules().handle(
        ModelPluginRulesRequest(correlation_id="test")
    )
    return result.rules


def facts(**over: object) -> ModelPluginRevisionFacts:
    base: dict[str, object] = {
        "name": OVERLAYS,
        "ref": "main",
        "installed_versions": ("0.1.30",),
        "merged_version": "0.1.30",
        "installed_revision": OLD,
        "merged_tip": TIP,
        "tip_in_installed": False,
        "rules": overlay_rules(),
    }
    base.update(over)
    return ModelPluginRevisionFacts.model_validate(base)


def version_only_verdict(f: ModelPluginRevisionFacts) -> EnumPluginDriftVerdict:
    """The old update-plugin judgement: drift only when a version string differs."""
    differs = any(v != f.merged_version for v in f.installed_versions)
    return EnumPluginDriftVerdict.DRIFT if differs else EnumPluginDriftVerdict.OK


def test_failing_control_same_version_older_revision_must_sync() -> None:
    f = facts()
    assert (
        version_only_verdict(f) == EnumPluginDriftVerdict.OK
    )  # the break: reads latest, never moves
    verdict = HandlerHostDriftPluginRevision().handle(f)
    assert verdict.verdict == EnumPluginDriftVerdict.DRIFT
    assert verdict.behind
    assert not verdict.version_differs
    assert verdict.observed == (
        "installed 0.1.30 merged 0.1.30; plugin-behind: installed revision "
        f"{OLD[:12]} lacks the merged tip {TIP[:12]} of origin/main"
    )
    assert verdict.sync_action == "install-plugin-tree"
    assert verdict.between_lanes is False


def test_golden_overlays_at_the_tip_is_ok() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(facts(tip_in_installed=True))
    assert verdict.verdict == EnumPluginDriftVerdict.OK
    assert verdict.observed == "installed 0.1.30 merged 0.1.30"
    assert verdict.sync_action is None


def test_golden_every_plugin_syncs_by_revision_not_only_omni() -> None:
    for name in (OVERLAYS, OMNI, "future-plugin@omninode-internal"):
        verdict = HandlerHostDriftPluginRevision().handle(facts(name=name))
        assert verdict.verdict == EnumPluginDriftVerdict.DRIFT, name
        assert verdict.sync_action == "install-plugin-tree", name


def test_golden_carried_commits_are_named() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(facts(carried_commits=2))
    assert verdict.observed.endswith(
        "of origin/main, carrying 2 commit(s) origin/main lacks"
    )


def test_golden_version_differs_behind_installs_the_tree_not_a_plain_update() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(installed_versions=("0.1.20",))
    )
    assert verdict.version_differs
    assert verdict.behind
    assert verdict.sync_action == "install-plugin-tree"


def test_golden_version_differs_with_unresolvable_revision_falls_back_to_update() -> (
    None
):
    f = facts(installed_versions=("0.1.20",), merged_tip=None, tip_in_installed=None)
    verdict = HandlerHostDriftPluginRevision().handle(f)
    assert verdict.verdict == EnumPluginDriftVerdict.DRIFT
    assert not verdict.behind
    assert verdict.sync_action == "update-plugin"


def test_golden_omni_rules_keep_their_order_and_lane_bounds() -> None:
    rules = overlay_rules()
    modified = rule_for(
        rules, f"plugin:{OMNI}", "installed 1 merged 1; tree modified against x"
    )
    stale = rule_for(
        rules, f"plugin:{OMNI}", "installed 1 merged 1; plugin-stale: ships x"
    )
    behind = rule_for(rules, f"plugin:{OMNI}", "installed 1 merged 1; plugin-behind: x")
    assert modified is not None
    assert stale is not None
    assert behind is not None
    assert (modified.action, modified.between_lanes) == ("repair-plugin", True)
    assert (stale.action, stale.between_lanes) == ("install-plugin-tree", True)
    assert (behind.action, behind.between_lanes) == ("install-plugin-tree", False)


def test_golden_tree_drift_settles_alone_before_behind() -> None:
    f = facts(
        name=OMNI, tree_drift="tree modified against shipped revision abc: changed=1"
    )
    verdict = HandlerHostDriftPluginRevision().handle(f)
    assert (
        verdict.observed
        == "installed 0.1.30 merged 0.1.30; tree modified against shipped revision abc: changed=1"
    )
    assert (verdict.sync_action, verdict.between_lanes) == ("repair-plugin", True)


def test_golden_stale_only_routes_to_the_tree_install_between_lanes_for_omni() -> None:
    f = facts(
        name=OMNI, tip_in_installed=True, stale_reason="installed plugin omni ships abc"
    )
    verdict = HandlerHostDriftPluginRevision().handle(f)
    assert verdict.observed.endswith("plugin-stale: installed plugin omni ships abc")
    assert (verdict.sync_action, verdict.between_lanes) == ("install-plugin-tree", True)


def test_error_unresolvable_revision_proves_nothing() -> None:
    for over in (
        {"merged_tip": None},
        {"installed_revision": None},
        {"tip_in_installed": None},
    ):
        verdict = HandlerHostDriftPluginRevision().handle(facts(**over))
        assert verdict.verdict == EnumPluginDriftVerdict.OK, over
        assert not verdict.behind


def test_error_malformed_facts_are_refused() -> None:
    with pytest.raises(ValidationError):
        facts(installed_versions=())
    with pytest.raises(ValidationError):
        facts(carried_commits=-1)
    with pytest.raises(ValidationError):
        facts(unknown_field=1)


def test_golden_version_bump_routes_to_update_plugin() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.476",
            tip_in_installed=False,
        )
    )
    assert verdict.sync_action == "update-plugin"
    assert verdict.version_differs
    assert verdict.behind
    assert verdict.between_lanes is False


def test_golden_version_bump_same_version_control_installs_tree() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.475",
            tip_in_installed=False,
        )
    )
    assert verdict.sync_action == "install-plugin-tree"


def test_golden_version_bump_tree_drift_wins() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.476",
            tree_drift="tree modified against x",
            tip_in_installed=False,
        )
    )
    assert verdict.sync_action == "repair-plugin"
    assert verdict.between_lanes is True


def test_golden_version_bump_not_behind_routes_to_update_plugin() -> None:
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.476",
            merged_tip=None,
            tip_in_installed=None,
        )
    )
    assert verdict.sync_action == "update-plugin"


def test_failing_control_version_bump_rule_removed_routes_to_the_tree_install() -> None:
    stripped = tuple(r for r in overlay_rules() if not r.version_differs)
    verdict = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.476",
            tip_in_installed=False,
            rules=stripped,
        )
    )
    assert verdict.sync_action == "install-plugin-tree"
    packaged = HandlerHostDriftPluginRevision().handle(
        facts(
            name=OMNI,
            installed_versions=("0.4.475",),
            merged_version="0.4.476",
            tip_in_installed=False,
        )
    )
    assert packaged.sync_action == "update-plugin"


def test_golden_rule_for_version_differs_false_does_not_match() -> None:
    rule = ModelPluginSyncRule(
        match="plugin:omni@omninode-internal",
        kinds=("plugin",),
        version_differs=True,
        sync_class="SAFE",
        action="update-plugin",
    )
    assert (
        rule_for(
            (rule,),
            "plugin:omni@omninode-internal",
            "installed 0.4.475 merged 0.4.476",
            False,
        )
        is None
    )


def test_error_rules_are_required_input_not_read_from_a_file() -> None:
    """The compute is handed its rules: facts without them are refused, and with none it only alerts."""
    payload = facts().model_dump()
    del payload["rules"]
    with pytest.raises(ValidationError):
        ModelPluginRevisionFacts.model_validate(payload)
    verdict = HandlerHostDriftPluginRevision().handle(facts(rules=()))
    assert verdict.verdict == EnumPluginDriftVerdict.DRIFT
    assert verdict.sync_action is None


def test_the_handler_reads_no_file_and_carries_no_purity_suppression() -> None:
    source = Path(handler_module.__file__ or "").read_text(encoding="utf-8")
    assert "import yaml" not in source
    assert "read_text" not in source
    assert "node-purity-ok" not in source
