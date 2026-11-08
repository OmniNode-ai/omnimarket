# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide a plugin's drift by installed REVISION, not by version string (OMN-20689).

Moved from the frozen internal host-drift package (RULING 2026-11-03T18:40:27Z): the plugin-behind and plugin-stale
wording, the version-or-revision composition and the plugin rules of the host-drift sync policy.
The probe that reads git stays host-side and hands this node typed facts.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from omnimarket.nodes.node_host_drift_plugin_revision_compute.models.model_plugin_revision import (
    EnumPluginDriftVerdict,
    ModelPluginRevisionFacts,
    ModelPluginRevisionVerdict,
    ModelPluginSyncRule,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "plugin_sync_policy.yaml"
RULES_SCHEMA = "host-drift-plugin-sync-rules.v1"
KIND_PLUGIN = "plugin"


def load_plugin_sync_rules(path: Path = POLICY_PATH) -> tuple[ModelPluginSyncRule, ...]:
    """The plugin sync rules from the overlay, in first-match order."""
    text = path.read_text(encoding="utf-8")  # node-purity-ok: overlay load
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict) or payload.get("schema") != RULES_SCHEMA:
        raise ValueError(f"unsupported plugin sync rules schema in {path.name}")
    rules = tuple(
        ModelPluginSyncRule.model_validate(r) for r in payload.get("rules", ())
    )
    for rule in rules:
        if (rule.sync_class == "SAFE") != bool(rule.action):
            raise ValueError(
                "a SAFE rule names an action; an ALERT_ONLY rule names none"
            )
    return rules


def rule_for(
    rules: tuple[ModelPluginSyncRule, ...], key: str, observed: str
) -> ModelPluginSyncRule | None:
    """The first plugin rule matching the key and observed text; None means ALERT_ONLY."""
    return next(
        (
            r
            for r in rules
            if fnmatchcase(key, r.match)
            and KIND_PLUGIN in r.kinds
            and (
                not r.observed_contains
                or any(m in observed for m in r.observed_contains)
            )
        ),
        None,
    )


def behind_note(facts: ModelPluginRevisionFacts) -> str:
    """Why the installed revision lacks its repository's merged tip, or "" (OMN-20497).

    Behind means the merged tip is not in the installed revision. A revision or tip the clone
    cannot resolve proves nothing and reads "" (placement's own verdict names that case).
    """
    revision, tip = facts.installed_revision, facts.merged_tip
    if revision is None or tip is None or facts.tip_in_installed is not False:
        return ""
    extra = (
        f", carrying {facts.carried_commits} commit(s) origin/{facts.ref} lacks"
        if facts.carried_commits
        else ""
    )
    return (
        f"plugin-behind: installed revision {revision[:12]} lacks the merged tip "
        f"{tip[:12]} of origin/{facts.ref}{extra}"
    )


class HandlerHostDriftPluginRevision:
    """Pure compute: typed probe facts to a drift verdict, its observed text and its sync action."""

    def __init__(self, rules: tuple[ModelPluginSyncRule, ...] | None = None) -> None:
        self._rules = rules if rules is not None else load_plugin_sync_rules()

    def handle(self, request: ModelPluginRevisionFacts) -> ModelPluginRevisionVerdict:
        facts = request
        observed = f"installed {','.join(facts.installed_versions)} merged {facts.merged_version}"
        version_differs = any(
            v != facts.merged_version for v in facts.installed_versions
        )
        behind = behind_note(facts)
        if facts.tree_drift:
            verdict, text = (
                EnumPluginDriftVerdict.DRIFT,
                f"{observed}; {facts.tree_drift}",
            )
        else:
            notes = [
                n
                for n in (
                    behind,
                    facts.stale_reason and f"plugin-stale: {facts.stale_reason}",
                )
                if n
            ]
            if notes or version_differs:
                verdict, text = (
                    EnumPluginDriftVerdict.DRIFT,
                    "; ".join([observed, *notes]),
                )
            else:
                verdict = EnumPluginDriftVerdict.OK
                text = f"{observed}; {facts.tree_ok}" if facts.tree_ok else observed
        action = None
        between = False
        if verdict == EnumPluginDriftVerdict.DRIFT:
            rule = rule_for(self._rules, f"plugin:{facts.name}", text)
            if rule is not None and rule.sync_class == "SAFE":
                action, between = rule.action, rule.between_lanes
        return ModelPluginRevisionVerdict(
            name=facts.name,
            verdict=verdict,
            observed=text,
            behind=bool(behind),
            version_differs=version_differs,
            sync_action=action,
            between_lanes=between,
        )
