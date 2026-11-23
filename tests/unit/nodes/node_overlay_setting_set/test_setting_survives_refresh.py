# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The value a set writes survives a refresh by the rule of the refresh change (a refresh never overwrites a live value).

The rule is the refresh_document of setting_paths.py at the head of omnimarket#6626 (67e9bfa4): the repository's document
with the live value put back at each path the record marks as operator-set. When that module is in the package, the real
one runs; until then the copy below, which is that function as read at that head, stands in for it.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    BOOT_SET,
    KEY,
    OLD_LAZY_START,
    OVERLAY_ID,
    PATH,
    build,
    contract,
    hotload_document,
)

pytestmark = pytest.mark.unit


def _vendored_refresh(
    repository: Mapping[str, JsonValue],
    live: Mapping[str, JsonValue],
    operator_paths: Sequence[str],
) -> Any:
    """refresh_document at omnimarket#6626 head 67e9bfa4, for mapping-only paths (every path a set here writes)."""

    class Refresh:
        def __init__(self, document: dict[str, JsonValue]) -> None:
            self.document = document

    def get_at(document: Any, path: str) -> Any:
        node = document
        for segment in path.split("/"):
            if not isinstance(node, dict) or segment not in node:
                return KeyError
            node = node[segment]
        return node

    merged: dict[str, JsonValue] = copy.deepcopy(dict(repository))
    for path in sorted(set(operator_paths)):
        held = get_at(dict(live), path)
        if held is KeyError:
            continue
        *parents, last = path.split("/")
        node: Any = merged
        for segment in parents:
            node = node.setdefault(segment, {})
        node[last] = copy.deepcopy(held)
    return Refresh(merged)


def _refresh_rule() -> Callable[..., Any]:
    try:
        module = importlib.import_module(
            "omnimarket.handlers.overlay_store.setting_paths"
        )
    except ImportError:
        return _vendored_refresh
    result: Callable[..., Any] = module.refresh_document
    return result


def _repository_document() -> dict[str, JsonValue]:
    """What the settings repository holds: the old boot set, and a setting nobody set through the store, changed since."""
    document = hotload_document()
    lanes: Any = document["lanes"]
    lanes["lane-b"]["allowed_environments"] = ["stage", "stage2"]
    return document


async def _written() -> dict[str, Any]:
    handler, bus, _ = build()
    await handler.handle(
        ModelOverlaySettingSetRequest.model_validate(
            {"overlay_id": OVERLAY_ID, "key": KEY, "value": BOOT_SET, "actor": ACTOR}
        )
    )
    payload: dict[str, Any] = json.loads(
        bus.on(contract()["event_bus"]["publish_topics"][0])[0][1]
    )
    return payload


async def test_the_written_value_is_kept_after_a_refresh() -> None:
    written = await _written()
    refreshed = _refresh_rule()(
        _repository_document(), written["document"], written["operator_paths"]
    )
    lanes = refreshed.document["lanes"]
    assert lanes["lane-a"]["lazy_start"] == BOOT_SET
    # A setting nobody set through the store takes the repository's value, as the refresh does.
    assert lanes["lane-b"]["allowed_environments"] == ["stage", "stage2"]


async def test_without_the_marked_path_the_same_refresh_replaces_the_value_positive_control() -> (
    None
):
    written = await _written()
    refreshed = _refresh_rule()(_repository_document(), written["document"], [])
    assert refreshed.document["lanes"]["lane-a"]["lazy_start"] == OLD_LAZY_START


async def test_the_marked_path_is_the_declared_settings_path() -> None:
    written = await _written()
    assert written["operator_paths"] == [PATH]
