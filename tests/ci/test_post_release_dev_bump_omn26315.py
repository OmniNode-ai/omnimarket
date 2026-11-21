# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dev stays ahead of the v0.4.324 release it was cut from (OMN-26315).

Publishing v0.4.324 from dev armed the release-identity gate against dev, because
dev's ``[project].version`` still read 0.4.324. The post-release bump moves dev to
0.4.325. These tests pin that outcome through the bump script's own decision
function, with no git tag lookup, so they run the same on a shallow checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.post_release_dev_bump import (
    ACTION_NOOP,
    decide,
    next_patch,
    parse_final_version,
    read_project_version,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_RELEASE = "0.4.324"


def test_bump_script_declares_no_bump_left_after_v0_4_324() -> None:
    """dev is strictly ahead of the published v0.4.324, so the script says noop."""
    dev_version = read_project_version(ROOT / "pyproject.toml")
    decision = decide(dev_version, PUBLISHED_RELEASE)
    assert decision.action == ACTION_NOOP, decision.reason


def test_dev_version_is_at_least_the_post_release_target() -> None:
    """dev reads the next patch above v0.4.324 or later, never the published version."""
    dev_version = read_project_version(ROOT / "pyproject.toml")
    assert parse_final_version(dev_version, label="dev version") >= parse_final_version(
        next_patch(PUBLISHED_RELEASE), label="post-release target"
    )
