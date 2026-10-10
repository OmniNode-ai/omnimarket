# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20074 — the two writer-app bump shapes no classifier admitted.

Fixtures are the real omniclaude bot diffs that the receipt-gate reusable's
writer step refused at omnibase_core a0cadfb0 with omnimarket 0.4.305:

* omniclaude#2644 (OMN-20710 post-merge plugin version bump): only the
  ``version`` of ``plugins/onex/.claude-plugin/plugin.json`` and of the onex
  entry in ``plugins/tools-marketplace/.claude-plugin/marketplace.json``
  moves, 2.4.59 -> 2.4.60. Refused as "changed path is not a dependency
  manifest or lockfile".
* omniclaude#2646 (OMN-13902 sibling lock refresh): the omnimarket git rev
  moves in ``tool.uv.override-dependencies``, ``tool.uv.sources`` and
  ``uv.lock``. Refused as "pyproject.toml changes outside
  version/dependency-pin keys: tool.uv.override-dependencies".

The refusals are the load-bearing half: an exemption surface only widens by the
exact keys a bump moves.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.occ_content_probe import (
    classify_dependency_pin_only,
    classify_plugin_manifest_version_only,
)

pytestmark = pytest.mark.unit

PLUGIN = "plugins/onex/.claude-plugin/plugin.json"
MARKET = "plugins/tools-marketplace/.claude-plugin/marketplace.json"
OLD_REV = "5a73a5accfa44dfbfd47778d8605615706b70dda"
NEW_REV = "917f721fcdfb30339cfb4532257d0d2e7a4cb4d4"


def _plugin(version: str, description: str = "Unified ONEX plugin") -> str:
    return json.dumps(
        {
            "name": "onex",
            "description": description,
            "version": version,
            "author": {"name": "OmniNode", "email": "dev@omninode.ai"},
        },
        indent=2,
    )


def _market(entry_version: str, *, market_version: str = "1.0.0", **extra: str) -> str:
    entry = {
        "name": "onex",
        "description": "Full ONEX architecture plugin",
        "version": entry_version,
        "author": {"name": "OmniNode", "email": "dev@omninode.ai"},
        "source": "./onex",
        "category": "development",
    }
    entry.update(extra)
    return json.dumps(
        {
            "name": "tools-marketplace",
            "version": market_version,
            "description": "dev marketplace",
            "owner": {"name": "OmniNode"},
            "plugins": [entry],
        },
        indent=2,
    )


def _real_2644() -> dict[str, tuple[str | None, str | None]]:
    return {
        PLUGIN: (_plugin("2.4.60"), _plugin("2.4.59")),
        MARKET: (_market("2.4.60"), _market("2.4.59")),
    }


def test_real_plugin_version_bump_is_admitted() -> None:
    verdict, reason = classify_plugin_manifest_version_only(
        [MARKET, PLUGIN], contents=_real_2644()
    )
    assert verdict is True, reason
    assert "2.4.60" in reason


def test_a_plugin_bump_that_also_edits_a_description_is_refused() -> None:
    contents = _real_2644()
    contents[PLUGIN] = (_plugin("2.4.60", "edited"), _plugin("2.4.59"))
    verdict, reason = classify_plugin_manifest_version_only(
        [MARKET, PLUGIN], contents=contents
    )
    assert verdict is False
    assert "description" in reason


def test_a_plugin_bump_that_edits_a_marketplace_entry_is_refused() -> None:
    contents = _real_2644()
    contents[MARKET] = (_market("2.4.60", source="./elsewhere"), _market("2.4.59"))
    verdict, reason = classify_plugin_manifest_version_only(
        [MARKET, PLUGIN], contents=contents
    )
    assert verdict is False
    assert "plugins.0.source" in reason


@pytest.mark.parametrize(
    ("head", "base"),
    [("2.4.58", "2.4.59"), ("2.4.59", "2.4.59-rc1"), ("next", "2.4.59")],
)
def test_a_version_that_does_not_increase_as_numbers_is_refused(
    head: str, base: str
) -> None:
    verdict, _ = classify_plugin_manifest_version_only(
        [PLUGIN], contents={PLUGIN: (_plugin(head), _plugin(base))}
    )
    assert verdict is False


@pytest.mark.parametrize(
    "path",
    [
        "plugins/onex/plugin.json",
        "plugins/onex/.claude-plugin/hooks.json",
        "src/pkg/.claude-plugin/plugin.json.bak",
    ],
)
def test_a_path_outside_the_plugin_manifests_is_refused(path: str) -> None:
    verdict, reason = classify_plugin_manifest_version_only(
        [path], contents={path: (_plugin("2.4.60"), _plugin("2.4.59"))}
    )
    assert verdict is False
    assert path in reason


@pytest.mark.parametrize(
    "contents",
    [
        {PLUGIN: (_plugin("2.4.60"), None)},
        {PLUGIN: (None, _plugin("2.4.59"))},
        {PLUGIN: ("{not json", _plugin("2.4.59"))},
        {PLUGIN: ("[]", "[]")},
    ],
)
def test_an_unreadable_added_deleted_or_non_object_manifest_is_refused(
    contents: dict[str, tuple[str | None, str | None]],
) -> None:
    verdict, _ = classify_plugin_manifest_version_only([PLUGIN], contents=contents)
    assert verdict is False


def test_no_changed_paths_and_no_moved_version_are_refused() -> None:
    assert classify_plugin_manifest_version_only([], contents={})[0] is False
    same = {PLUGIN: (_plugin("2.4.59"), _plugin("2.4.59"))}
    verdict, reason = classify_plugin_manifest_version_only([PLUGIN], contents=same)
    assert verdict is False
    assert "no plugin version moved" in reason


def test_too_many_manifests_are_refused() -> None:
    paths = [f"plugins/p{i}/.claude-plugin/plugin.json" for i in range(6)]
    contents = {path: (_plugin("2.4.60"), _plugin("2.4.59")) for path in paths}
    assert classify_plugin_manifest_version_only(paths, contents=contents)[0] is False


def _pyproject(override_rev: str, source_rev: str, *, scripts: str = "") -> str:
    return f"""\
[project]
name = "omninode-claude"
version = "0.31.0"
dependencies = ["omnibase-core>=0.47.0,<0.48.0"]
{scripts}
[tool.uv]
override-dependencies = [
    "omnibase-core>=0.47.0,<0.48.0",
    "omnimarket @ git+https://github.com/OmniNode-ai/omnimarket.git@{override_rev}",
]

[tool.uv.sources]
omnimarket = {{ git = "https://github.com/OmniNode-ai/omnimarket.git", rev = "{source_rev}" }}
"""


def test_real_sibling_lock_refresh_is_dependency_pin_only() -> None:
    verdict, reason = classify_dependency_pin_only(
        ["pyproject.toml", "uv.lock"],
        pyproject_head=_pyproject(NEW_REV, NEW_REV),
        pyproject_base=_pyproject(OLD_REV, OLD_REV),
    )
    assert verdict is True, reason
    assert "tool.uv.override-dependencies" in reason


def test_an_override_pin_beside_a_behaviour_change_is_still_refused() -> None:
    verdict, reason = classify_dependency_pin_only(
        ["pyproject.toml", "uv.lock"],
        pyproject_head=_pyproject(
            NEW_REV, NEW_REV, scripts='[project.scripts]\nonex = "pkg:main"\n'
        ),
        pyproject_base=_pyproject(OLD_REV, OLD_REV),
    )
    assert verdict is False
    assert "project.scripts" in reason
