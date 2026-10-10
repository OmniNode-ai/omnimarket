# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: pin the reusable that derives the writer's pin-only exemption in-job."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.occ_content_probe import classify_dependency_pin_only

pytestmark = pytest.mark.unit

CALLER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)


def test_caller_pins_the_reusable_that_derives_writer_pin_only_in_job() -> None:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    # The pin moved to a descendant that keeps this step: omnibase_core#1914 (OMN-20074).
    assert caller["jobs"]["repo-evidence"]["uses"].endswith(
        "@fb0c6c2117d5868a398b0920cd0048d0824415b1"
    )


def test_pinned_verifier_ships_the_pin_only_classifier() -> None:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    version = tuple(
        int(part)
        for part in caller["jobs"]["repo-evidence"]["with"]["verifier-version"].split(
            "."
        )
    )
    assert version >= (0, 4, 294)


def test_post_release_bump_shape_is_pin_only() -> None:
    base = (
        "[project]\n"
        'name = "omnimarket"\n'
        'version = "0.4.304"\n'
        'dependencies = ["pyyaml>=6.0"]\n'
    )
    head = (
        "[project]\n"
        'name = "omnimarket"\n'
        'version = "0.4.305"\n'
        'dependencies = ["pyyaml>=6.0"]\n'
    )
    paths = ["pyproject.toml", "uv.lock"]
    pin_only, reason = classify_dependency_pin_only(
        paths, pyproject_head=head, pyproject_base=base
    )
    assert pin_only is True
    assert "project.version" in reason

    pin_only, _ = classify_dependency_pin_only(
        [*paths, "src/omnimarket/x.py"], pyproject_head=head, pyproject_base=base
    )
    assert pin_only is False
