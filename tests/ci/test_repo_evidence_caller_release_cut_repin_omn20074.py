# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20074: pin the reusable that derives the writer's release-cut exemption."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket import occ_content_probe

pytestmark = pytest.mark.unit

CALLER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)


def test_caller_pins_the_reusable_that_derives_writer_release_cut_at_head() -> None:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    assert caller["jobs"]["repo-evidence"]["uses"].endswith(
        "@fb0c6c2117d5868a398b0920cd0048d0824415b1"
    )


def test_pinned_verifier_ships_the_release_cut_classifiers() -> None:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    version = tuple(
        int(part)
        for part in caller["jobs"]["repo-evidence"]["with"]["verifier-version"].split(
            "."
        )
    )
    assert version >= (0, 4, 294)
    assert callable(occ_content_probe.is_release_artifact_only_diff)
    assert callable(occ_content_probe.classify_dependency_pin_only)


def test_release_artifact_only_diff_shape() -> None:
    assert (
        occ_content_probe.is_release_artifact_only_diff(
            ["CHANGELOG.md", "pyproject.toml", "uv.lock"]
        )
        is True
    )
    assert (
        occ_content_probe.is_release_artifact_only_diff(
            ["CHANGELOG.md", "src/omnimarket/__init__.py"]
        )
        is False
    )
