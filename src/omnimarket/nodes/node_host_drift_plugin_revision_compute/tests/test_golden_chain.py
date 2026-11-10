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

from omnimarket.nodes.node_host_drift_plugin_revision_compute.handlers.handler_plugin_revision import (
    HandlerHostDriftPluginRevision,
    load_plugin_sync_rules,
    rule_for,
)
from omnimarket.nodes.node_host_drift_plugin_revision_compute.models.model_plugin_revision import (
    EnumPluginDriftVerdict,
    ModelPluginRevisionFacts,
)

OVERLAYS = "onex-overlays@omninode-internal"
OMNI = "omni@omninode-internal"
OLD, TIP = "a" * 40, "b" * 40


def facts(**over: object) -> ModelPluginRevisionFacts:
    base: dict[str, object] = {
        "name": OVERLAYS,
        "ref": "main",
        "installed_versions": ("0.1.30",),
        "merged_version": "0.1.30",
        "installed_revision": OLD,
        "merged_tip": TIP,
        "tip_in_installed": False,
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
    rules = load_plugin_sync_rules()
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


def test_error_a_rule_set_with_a_safe_rule_and_no_action_is_refused(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        "schema: host-drift-plugin-sync-rules.v1\n"
        "rules:\n  - {match: 'plugin:*', kinds: [plugin], sync_class: SAFE}\n"
    )
    with pytest.raises(ValueError, match="names an action"):
        load_plugin_sync_rules(bad)
    bad.write_text("schema: nope\nrules: []\n")
    with pytest.raises(ValueError, match="unsupported"):
        load_plugin_sync_rules(bad)
