# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23851: the rule and ticket-scope overlays are read from the overlay store root first, then the plugin path.

Pattern: the delegation routing store-first loader (OMN-23826). A broken store document refuses with its key
and path and never falls through to the plugin copy behind it. Each refusal has its positive control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers.goal_scope_overlay import (
    GOAL_RULE_OVERLAY_ID,
    PLUGIN_OVERLAY_SOURCE,
    TICKET_SCOPE_OVERLAY_ID,
    GoalScopeOverlayError,
    find_goal_overlay,
    load_goal_ticket_rule,
    load_ticket_scope,
)

pytestmark = pytest.mark.unit

COMMON = (
    'overlay_version: "1.0.0"\nticket: OMN-22570\nruling: "fixture"\n'
    "endpoint: https://linear.invalid/graphql\ncache_seconds: 60\nreads_per_tick: 4\nread_timeout_seconds: 5\n"
)


def rule_text(*roots: str, max_depth: int = 6) -> str:
    return (
        COMMON
        + f"max_depth: {max_depth}\nticket_roots:\n"
        + "".join(f"- {r}\n" for r in roots)
    )


def scope_text(*names: str) -> str:
    return (
        COMMON
        + "project_ids: []\nproject_names:\n"
        + "".join(f"- {n}\n" for n in names)
    )


def put(root: Path, overlay_id: str, text: str) -> Path:
    path = root / overlay_id / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def env(
    tmp_path: Path, *, store: Path | None = None, plugin: Path | None = None
) -> dict[str, str]:
    """An environment whose store root is ``store`` and whose installed onex-overlays plugin keeps its overlays in ``plugin``."""
    config = tmp_path / "claude-config"
    if plugin is not None:
        (config / "plugins").mkdir(parents=True, exist_ok=True)
        (config / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {"plugins": {"onex-overlays@m": [{"installPath": str(plugin.parent)}]}}
            ),
            encoding="utf-8",
        )
    out = {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(tmp_path / "no-home")}
    if store is not None:
        out["ONEX_SKILL_OVERLAY_ROOTS"] = str(store)
    return out


def test_store_first_the_store_document_wins_over_the_plugin_copy(
    tmp_path: Path,
) -> None:
    store, plugin = tmp_path / "store", tmp_path / "plugin" / "overlays"
    put(store, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    put(plugin, GOAL_RULE_OVERLAY_ID, rule_text("OMN-2"))
    rule = load_goal_ticket_rule(env(tmp_path, store=store, plugin=plugin))
    assert rule is not None
    assert rule.roots == {"OMN-1"}
    assert rule.max_depth == 6
    assert rule.reads_per_tick == 4


def test_store_first_the_plugin_path_answers_only_when_the_store_holds_none(
    tmp_path: Path,
) -> None:
    store, plugin = tmp_path / "store", tmp_path / "plugin" / "overlays"
    put(plugin, GOAL_RULE_OVERLAY_ID, rule_text("OMN-2"))
    put(plugin, TICKET_SCOPE_OVERLAY_ID, scope_text("Factory"))
    store.mkdir()
    environ = env(tmp_path, store=store, plugin=plugin)
    found = find_goal_overlay(GOAL_RULE_OVERLAY_ID, environ)
    assert found is not None
    assert found[1] == PLUGIN_OVERLAY_SOURCE
    rule, scope = load_goal_ticket_rule(environ), load_ticket_scope(environ)
    assert rule is not None
    assert rule.roots == {"OMN-2"}
    assert scope is not None
    assert scope.project_names == {"Factory"}


def test_store_first_the_first_store_root_that_holds_one_wins(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    put(second, GOAL_RULE_OVERLAY_ID, rule_text("OMN-3"))
    environ = env(tmp_path, store=first)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = f"{first}:{second}"
    rule = load_goal_ticket_rule(environ)
    assert rule is not None
    assert rule.roots == {"OMN-3"}


def test_neither_source_holding_the_document_is_none_not_an_error(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    assert load_goal_ticket_rule(env(tmp_path, store=store)) is None
    assert load_ticket_scope(env(tmp_path, store=store)) is None


def test_broken_store_document_refuses_with_its_key_and_path_and_never_falls_through(
    tmp_path: Path,
) -> None:
    store, plugin = tmp_path / "store", tmp_path / "plugin" / "overlays"
    path = put(
        store, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1").replace("max_depth: 6\n", "")
    )
    put(plugin, GOAL_RULE_OVERLAY_ID, rule_text("OMN-2"))
    environ = env(tmp_path, store=store, plugin=plugin)
    with pytest.raises(GoalScopeOverlayError) as raised:
        load_goal_ticket_rule(environ)
    message = str(raised.value)
    assert "ONEX_SKILL_OVERLAY_ROOTS" in message
    assert str(path) in message
    assert "lacks max_depth" in message
    # the positive control: with the store document repaired the very same environment loads it
    put(store, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    rule = load_goal_ticket_rule(environ)
    assert rule is not None
    assert rule.roots == {"OMN-1"}


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("not: [a mapping\n", "is unreadable"),
        ("- a list\n", "must be a mapping"),
        (rule_text("OMN-1") + "surprise: 1\n", "carries unknown surprise"),
        (rule_text(), "at least one OMN-<n> ticket"),
        (rule_text("not-a-ticket"), "at least one OMN-<n> ticket"),
        (
            rule_text("OMN-1").replace("cache_seconds: 60", "cache_seconds: soon"),
            "cache_seconds must be a whole number",
        ),
        (rule_text("OMN-1", max_depth=0), "max_depth must be a whole number"),
    ],
)
def test_broken_rule_document_names_what_is_wrong(
    tmp_path: Path, text: str, why: str
) -> None:
    store = tmp_path / "store"
    put(store, GOAL_RULE_OVERLAY_ID, text)
    with pytest.raises(GoalScopeOverlayError, match=why):
        load_goal_ticket_rule(env(tmp_path, store=store))


def test_broken_scope_document_that_names_no_project_refuses_and_a_broken_plugin_copy_names_its_key(
    tmp_path: Path,
) -> None:
    store, plugin = tmp_path / "store", tmp_path / "plugin" / "overlays"
    put(store, TICKET_SCOPE_OVERLAY_ID, scope_text())
    with pytest.raises(GoalScopeOverlayError, match="name at least one project"):
        load_ticket_scope(env(tmp_path, store=store))
    empty = tmp_path / "empty-store"
    empty.mkdir()
    put(plugin, TICKET_SCOPE_OVERLAY_ID, scope_text())
    with pytest.raises(GoalScopeOverlayError, match="onex-overlays plugin path"):
        load_ticket_scope(env(tmp_path, store=empty, plugin=plugin))


def test_the_shipped_plugin_documents_load_as_the_node_reads_them(
    tmp_path: Path,
) -> None:
    """The rule overlay's real shape (comments, a list of roots) loads: the plugin's goal_scope_ticket_rule and ticket_scope."""
    store = tmp_path / "store"
    put(
        store,
        GOAL_RULE_OVERLAY_ID,
        "# a comment\n" + rule_text("OMN-22478", "OMN-21288", "OMN-19787"),
    )
    rule = load_goal_ticket_rule(env(tmp_path, store=store))
    assert rule is not None
    assert rule.roots == {"OMN-22478", "OMN-21288", "OMN-19787"}
