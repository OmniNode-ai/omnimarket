# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dev reads a version above the v0.4.329 release it was cut from (OMN-26733).

With dev at 0.4.329, the release-identity hook refused every commit that touched
src/. The post-release bump to 0.4.330 moved dev above the tag. The tests read the
checked-in version and the bump script's own decision, with no git tag lookup, so
they run the same on a shallow checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from scripts.ci.post_release_dev_bump import (
    ACTION_NOOP,
    decide,
    read_project_version,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_RELEASE = "0.4.329"


def test_dev_version_is_above_v0_4_329() -> None:
    dev_version = read_project_version(ROOT / "pyproject.toml")
    assert Version(dev_version) > Version(PUBLISHED_RELEASE), dev_version


def test_bump_script_has_no_bump_left_after_v0_4_329() -> None:
    dev_version = read_project_version(ROOT / "pyproject.toml")
    decision = decide(dev_version, PUBLISHED_RELEASE)
    assert decision.action == ACTION_NOOP, decision.reason


def test_known_bad_version_equal_to_the_release_is_not_ahead() -> None:
    """Positive control: a dev that still reads the published version fails the check."""
    assert not Version(PUBLISHED_RELEASE) > Version(PUBLISHED_RELEASE)
    assert decide(PUBLISHED_RELEASE, PUBLISHED_RELEASE).action != ACTION_NOOP
