# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep repository-owned hooks local after OCC retirement S8 (OMN-20068)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# Hooks whose behaviour lives in a repository other than onex_change_control.
MOVED_HOOK_IDS = frozenset({"no-untracked-todos"})


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), "expected a YAML mapping"
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str), "expected string mapping keys"
        result[key] = item
    return result


def _mappings(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list), "expected a YAML list of mappings"
    return [_mapping(item) for item in value]


def _repos() -> list[dict[str, object]]:
    config = _mapping(yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8")))
    return _mappings(config["repos"])


def _matching_hooks(hook_id: str) -> list[tuple[str, dict[str, object]]]:
    matches: list[tuple[str, dict[str, object]]] = []
    for repo in _repos():
        repo_name = repo["repo"]
        assert isinstance(repo_name, str), "expected a string repository name"
        for hook in _mappings(repo.get("hooks", [])):
            if hook.get("id") == hook_id:
                matches.append((repo_name, hook))
    return matches


def test_no_untracked_todos_is_a_local_hook_running_the_core_handler() -> None:
    """Catch duplicate TODO hooks or a return to the external implementation."""
    matches = _matching_hooks("no-untracked-todos")
    assert len(matches) == 1
    repo, hook = matches[0]
    assert repo == "local"
    entry = hook["entry"]
    assert isinstance(entry, str)
    assert "omnibase_core.handlers.handler_todo_format" in entry
    assert "check-todo-format" not in entry


def test_no_moved_hook_is_in_the_onex_change_control_block() -> None:
    """Catch moved hooks returning to the OCC repository block."""
    for repo in _repos():
        repo_name = repo["repo"]
        assert isinstance(repo_name, str)
        if repo_name.endswith("onex_change_control"):
            for hook in _mappings(repo.get("hooks", [])):
                hook_id = hook["id"]
                assert isinstance(hook_id, str)
                assert hook_id not in MOVED_HOOK_IDS
