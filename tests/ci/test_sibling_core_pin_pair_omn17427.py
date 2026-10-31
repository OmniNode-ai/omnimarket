# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: omnimarket's omnibase-core pin is the one its omnibase_infra pin names.

omnibase_infra reads omnibase-core from a dev merge tag in its own
``[tool.uv.sources]``, and uv honours a git dependency's sources. When
omnimarket moves its core tag and leaves infra on a commit that names another
core tag, the two sources disagree and the Sibling Release Compatibility Gate
(OMN-20381) refuses the head with "Requirements contain conflicting URLs for
package omnibase-core". Eight core-only bump PRs (omnimarket#4588 to #4624)
stalled on exactly that.

``INFRA_CORE_TAGS`` records, oldest first, each omnibase_infra commit this
repository has pinned and the core dev merge tag that commit's pyproject.toml
pins, spelled here so the test runs without reading another clone. The order
that works is: omnibase_infra moves its core tag, then one omnimarket change
moves both tags, relocks and adds the row below.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# omnibase_infra commit (sha12) -> the omnibase_core dev merge tag its
# pyproject.toml [tool.uv.sources] pins at that commit.
# 091013fd7adb: omnibase_infra dev at omnimarket#4598's pin.
# 4284f5bf5587: omnibase_infra#5483, which moves infra to core fc6159b98026.
INFRA_CORE_TAGS: tuple[tuple[str, str], ...] = (
    ("091013fd7adb", "dev-20261030152335-66d35d2283c3"),
    ("4284f5bf5587", "dev-20261031000524-fc6159b98026"),
)

_GIT_SOURCE = re.compile(
    r"^https://github\.com/OmniNode-ai/(?P<repo>[a-z_]+)\.git"
    r"\?tag=(?P<tag>dev-[0-9]{14}-(?P<sha12>[0-9a-f]{12}))#(?P<sha>[0-9a-f]{40})$"
)


def _locked_dev_tag(name: str) -> tuple[str, str]:
    """``(tag, full sha)`` of the one ``name`` entry in uv.lock, a dev-merge-tag git source."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    entries = [pkg for pkg in lock["package"] if pkg["name"] == name]
    assert len(entries) == 1, f"{name}: expected one uv.lock entry, got {len(entries)}"
    match = _GIT_SOURCE.match(entries[0]["source"].get("git", ""))
    assert match is not None, (
        f"{name}: not a dev-merge-tag git source: {entries[0]['source']}"
    )
    assert match["sha"].startswith(match["sha12"]), f"{name}: tag names another commit"
    return match["tag"], match["sha"]


def _pyproject_tag(name: str) -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(pyproject["tool"]["uv"]["sources"][name]["tag"])


def core_tag_conflict(infra_sha: str, core_tag: str) -> str | None:
    """Why ``core_tag`` cannot resolve beside omnibase_infra at ``infra_sha``, or None."""
    recorded = dict(INFRA_CORE_TAGS)
    infra_core = recorded.get(infra_sha[:12])
    if infra_core is None:
        return (
            f"omnibase_infra {infra_sha[:12]} has no recorded core tag; add its "
            "pyproject.toml omnibase-core tag to INFRA_CORE_TAGS"
        )
    if infra_core != core_tag:
        return (
            f"omnibase_infra {infra_sha[:12]} pins omnibase-core {infra_core}, "
            f"omnimarket pins {core_tag}; move both pins in one change"
        )
    return None


def test_a_core_only_bump_is_named_as_the_conflict() -> None:
    """Positive control: core fc6159b98026 beside infra 091013fd7adb is refused."""
    assert core_tag_conflict(
        "091013fd7adbba907d4df57b007ca58ae0b2b660", "dev-20261031000524-fc6159b98026"
    ) == (
        "omnibase_infra 091013fd7adb pins omnibase-core "
        "dev-20261030152335-66d35d2283c3, omnimarket pins "
        "dev-20261031000524-fc6159b98026; move both pins in one change"
    )
    assert "no recorded core tag" in (core_tag_conflict("0" * 40, "x") or "")


def test_pyproject_and_lock_name_the_same_infra_and_core_tags() -> None:
    for name in ("omnibase-infra", "omnibase-core"):
        assert _locked_dev_tag(name)[0] == _pyproject_tag(name)


def test_the_core_pin_is_the_one_the_infra_pin_names() -> None:
    _, infra_sha = _locked_dev_tag("omnibase-infra")
    core_tag, _ = _locked_dev_tag("omnibase-core")
    assert core_tag_conflict(infra_sha, core_tag) is None


def test_omnimarket_pins_the_newest_recorded_infra_and_core_pair() -> None:
    """The lock moves with the newest recorded row: infra#5483 and core fc6159b98026."""
    infra_tag, infra_sha = _locked_dev_tag("omnibase-infra")
    core_tag, _ = _locked_dev_tag("omnibase-core")
    assert (infra_sha[:12], core_tag) == INFRA_CORE_TAGS[-1]
    assert infra_tag == "dev-20261031054837-4284f5bf5587"
