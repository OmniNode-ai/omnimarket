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
# 3f93a2cc6f6c: omnibase_infra#5536, which moves infra to core 5bb91ae7e720.
# 858c2c689461: omnibase_infra dev at the core 2080333acfaf pin (OMN-21826).
# beb08a83c93e: omnibase_infra#5602 (OMN-19986, OMN-19991), same core pin.
# 33b470fda2b8: omnibase_infra#5768 (OMN-22899, vendors view_seam_readings and its grant), core b002ec3ceab1.
# 8b4382a1606d: omnibase_infra#5818 vendors node_projection_delegation 0068; core pin unchanged.
# 0ddea3131fd0: omnibase_infra#5833 vendors the gate store migrations (OMN-23055); core is unchanged.
# 25ae57b628a4: omnibase_infra#5890 (OMN-23705) vendors the work ledger latest-state migrations 0011 and 0012 and the runtime grant; core is unchanged.
# 440ae9e031dc: omnibase_infra#5815 (OMN-18595, OMN-23424) moves infra to core cd128b790ec6, which carries the ticket validator (omnibase_core#2281).
# 6e29faa59eeb: omnibase_infra#5883 adds tenant_id to ModelCiBusLane; core is unchanged.
# a349fbf4478e: omnibase_infra#5894 (OMN-23838) moves infra to core 6cbe67b216fe, which carries the delegation routing order models (omnibase_core#2309).
INFRA_CORE_TAGS: tuple[tuple[str, str], ...] = (
    ("091013fd7adb", "dev-20261030152335-66d35d2283c3"),
    ("4284f5bf5587", "dev-20261031000524-fc6159b98026"),
    ("3f93a2cc6f6c", "dev-20261031202655-5bb91ae7e720"),
    ("858c2c689461", "dev-20261101203942-2080333acfaf"),
    ("beb08a83c93e", "dev-20261101203942-2080333acfaf"),
    ("33b470fda2b8", "dev-20261104102410-b002ec3ceab1"),
    ("e64fe3502b45", "dev-20261108014124-2fe30fd9dc47"),
    ("8b4382a1606d", "dev-20261108014124-2fe30fd9dc47"),
    ("440ae9e031dc", "dev-20261109065232-cd128b790ec6"),
    ("0ddea3131fd0", "dev-20261109065232-cd128b790ec6"),
    ("6e29faa59eeb", "dev-20261109065232-cd128b790ec6"),
    ("25ae57b628a4", "dev-20261109065232-cd128b790ec6"),
    ("a349fbf4478e", "dev-20261111160725-6cbe67b216fe"),
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
    """The lock moves with the newest recorded row: infra a349fbf4478e and core 6cbe67b216fe."""
    infra_tag, infra_sha = _locked_dev_tag("omnibase-infra")
    core_tag, _ = _locked_dev_tag("omnibase-core")
    assert (infra_sha[:12], core_tag) == INFRA_CORE_TAGS[-1]
    assert infra_tag == "dev-20261111190933-a349fbf4478e"
