# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal's ticket rule is read from the injected settings store first, then the overlay file root, then the plugin path.

The store is the omnibase_spi overlay store protocol (the one the settings store slice injects); a broken store document refuses with its key
and never falls through to the files. Each refusal has its positive control.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from omnimarket.handlers.goal_scope_overlay import (
    GOAL_RULE_OVERLAY_ID,
    SETTINGS_STORE_SOURCE,
    GoalScopeOverlayError,
    installed_plugin_overlay_roots,
    load_goal_ticket_rule,
)
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_goal_scope_admission_compute.tests.test_goal_scope_overlay_store_first import (
    env,
    put,
    rule_text,
)

pytestmark = pytest.mark.unit

LOADED_AT = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def document(*roots: str) -> dict[str, JsonValue]:
    return {
        "overlay_version": "1.0.0",
        "ticket": "OMN-22570",
        "ruling": "fixture",
        "endpoint": "https://linear.invalid/graphql",
        "cache_seconds": 60,
        "reads_per_tick": 4,
        "read_timeout_seconds": 5,
        "max_depth": 6,
        "ticket_roots": list(roots),
    }


def stored(doc: dict[str, JsonValue]) -> InMemoryOverlayStore:
    """A store holding ``doc`` as the current version of the goal rule overlay."""
    store = InMemoryOverlayStore()
    record: dict[str, Any] = ModelOverlayStoreRecord(
        overlay_id=GOAL_RULE_OVERLAY_ID,
        digest=document_digest(doc),
        document=doc,
        source_ref=f"{GOAL_RULE_OVERLAY_ID}@abc123",
        loaded_by="loader",
        loaded_at=LOADED_AT,
    ).model_dump(mode="json")
    assert asyncio.run(store.write_version(record))
    return store


def test_the_store_document_wins_over_the_file_root_and_the_plugin_path(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "files"
    environ = env(tmp_path, store=file_root, plugin=tmp_path / "plugin" / "install")
    put(file_root, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    put(
        installed_plugin_overlay_roots(environ)[0],
        GOAL_RULE_OVERLAY_ID,
        rule_text("OMN-2"),
    )
    rule = load_goal_ticket_rule(environ, store=stored(document("OMN-30", "OMN-31")))
    assert rule is not None
    assert rule.roots == {"OMN-30", "OMN-31"}
    assert rule.max_depth == 6


def test_a_broken_store_document_refuses_with_its_key_and_never_falls_through(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "files"
    put(file_root, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    broken = document("not-a-ticket")
    with pytest.raises(GoalScopeOverlayError, match=SETTINGS_STORE_SOURCE) as raised:
        load_goal_ticket_rule(env(tmp_path, store=file_root), store=stored(broken))
    assert GOAL_RULE_OVERLAY_ID in str(raised.value)
    assert "at least one OMN-<n> ticket" in str(raised.value)
    # positive control: the same file root alone loads
    control = load_goal_ticket_rule(env(tmp_path, store=file_root))
    assert control is not None
    assert control.roots == {"OMN-1"}


def test_a_store_record_without_a_document_refuses_and_never_falls_through(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "files"
    put(file_root, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    store = InMemoryOverlayStore()
    record: dict[str, Any] = {
        "overlay_id": GOAL_RULE_OVERLAY_ID,
        "loaded_at": LOADED_AT.isoformat(),
    }
    assert asyncio.run(store.write_version(record))
    with pytest.raises(GoalScopeOverlayError, match=SETTINGS_STORE_SOURCE):
        load_goal_ticket_rule(env(tmp_path, store=file_root), store=store)


def test_an_empty_store_reads_the_file_root_then_the_plugin_path(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "files"
    environ = env(tmp_path, store=file_root, plugin=tmp_path / "plugin" / "install")
    put(file_root, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    put(
        installed_plugin_overlay_roots(environ)[0],
        GOAL_RULE_OVERLAY_ID,
        rule_text("OMN-2"),
    )
    empty = InMemoryOverlayStore()
    from_files = load_goal_ticket_rule(environ, store=empty)
    assert from_files is not None
    assert from_files.roots == {"OMN-1"}
    no_files = tmp_path / "no-files"
    no_files.mkdir()
    from_plugin = load_goal_ticket_rule(
        {**environ, "ONEX_SKILL_OVERLAY_ROOTS": str(no_files)}, store=empty
    )
    assert from_plugin is not None
    assert from_plugin.roots == {"OMN-2"}
    bare = tmp_path / "bare"
    assert load_goal_ticket_rule(env(bare, store=no_files), store=empty) is None


def test_with_no_store_given_the_files_are_read_as_before(tmp_path: Path) -> None:
    file_root = tmp_path / "files"
    put(file_root, GOAL_RULE_OVERLAY_ID, rule_text("OMN-1"))
    rule = load_goal_ticket_rule(env(tmp_path, store=file_root))
    assert rule is not None
    assert rule.roots == {"OMN-1"}
