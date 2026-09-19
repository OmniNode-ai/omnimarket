# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18848 — ``classify_dependency_pin_only`` acceptance rules.

The function is the DERIVED half of the no-companion-required exemption: it
decides, from the diff alone, whether a PR is a pure dependency bump and so
carries no behavioural claim for an OCC companion to attest. Because a false
positive here exempts a real change from evidence, every test below that
asserts a refusal is load-bearing — the passes are the easy half.

Fixture shape is the real wedged record, ``omnimarket#2685``: a post-release
bump to 0.4.133 whose entire diff is ``pyproject.toml`` (+1/-1) and ``uv.lock``
(+1/-1).
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import classify_dependency_pin_only

pytestmark = pytest.mark.unit

BASE_MANIFEST = """\
[project]
name = "omnimarket"
version = "0.4.132"
dependencies = ["omnibase_core==0.47.17"]

[project.scripts]
omnimarket = "omnimarket.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/omnimarket"]
"""

PIN_ONLY_HEAD = BASE_MANIFEST.replace('version = "0.4.132"', 'version = "0.4.133"')
BUMPED_DEP_HEAD = BASE_MANIFEST.replace(
    "omnibase_core==0.47.17", "omnibase_core==0.47.18"
)

PIN_ONLY_PATHS = ("pyproject.toml", "uv.lock")


class TestPinOnlyAccepted:
    def test_the_wedged_shape_is_pin_only(self) -> None:
        """AC1 — a version bump touching manifest and lockfile only."""
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS, pyproject_head=PIN_ONLY_HEAD, pyproject_base=BASE_MANIFEST
        )
        assert ok, reason
        assert "project.version" in reason

    def test_a_dependency_repin_is_pin_only(self) -> None:
        """The cascade's other shape: the sibling pin moves, not our version."""
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS, pyproject_head=BUMPED_DEP_HEAD, pyproject_base=BASE_MANIFEST
        )
        assert ok, reason
        assert "project.dependencies" in reason

    def test_a_lockfile_only_diff_needs_no_manifest_read(self) -> None:
        """A relock with no manifest change has no manifest to compare, and
        a lockfile cannot carry a behavioural claim on its own."""
        ok, _ = classify_dependency_pin_only(
            ("uv.lock",), pyproject_head=None, pyproject_base=None
        )
        assert ok


class TestRefusals:
    """Each of these is a way the exemption could leak. None may pass."""

    def test_a_source_file_alongside_the_lockfile_is_refused(self) -> None:
        """AC3 — the shape that would smuggle a real change through."""
        ok, reason = classify_dependency_pin_only(
            ("uv.lock", "src/omnimarket/handler.py"),
            pyproject_head=None,
            pyproject_base=None,
        )
        assert not ok
        assert "src/omnimarket/handler.py" in reason

    @pytest.mark.parametrize(
        ("mutation", "expected_key"),
        [
            (
                (
                    'omnimarket = "omnimarket.cli:main"',
                    'omnimarket = "omnimarket.cli:other"',
                ),
                "project.scripts.omnimarket",
            ),
            (
                ('requires = ["hatchling"]', 'requires = ["hatchling", "setuptools"]'),
                "build-system.requires",
            ),
            (
                (
                    'packages = ["src/omnimarket"]',
                    'packages = ["src/omnimarket", "src/extra"]',
                ),
                "tool.hatch.build.targets.wheel.packages",
            ),
        ],
        ids=["entry-point", "build-config", "force-include-shaped"],
    )
    def test_a_non_pin_manifest_edit_is_refused(
        self, mutation: tuple[str, str], expected_key: str
    ) -> None:
        """AC2 — a bump that ALSO edits behaviour is not a bump. Each case
        rides along with a legitimate version change, which is exactly how such
        an edit would reach this function in the wild."""
        head = PIN_ONLY_HEAD.replace(*mutation)
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS, pyproject_head=head, pyproject_base=BASE_MANIFEST
        )
        assert not ok
        assert expected_key in reason

    def test_an_empty_changed_file_list_is_refused(self) -> None:
        """An unobservable diff is not an empty one: the probe returns ``()``
        when it could not read the PR at all."""
        ok, reason = classify_dependency_pin_only(
            (), pyproject_head=None, pyproject_base=None
        )
        assert not ok
        assert "unobservable" in reason

    def test_an_unreadable_manifest_is_refused(self) -> None:
        """'I could not look' must never read as 'nothing else changed'."""
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS, pyproject_head=PIN_ONLY_HEAD, pyproject_base=None
        )
        assert not ok
        assert "unreadable" in reason

    def test_a_malformed_manifest_is_refused(self) -> None:
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS,
            pyproject_head="[project\nname =",
            pyproject_base=BASE_MANIFEST,
        )
        assert not ok
        assert "does not parse" in reason

    def test_a_nested_non_manifest_path_is_refused(self) -> None:
        ok, _ = classify_dependency_pin_only(
            ("pyproject.toml", "uv.lock", "docs/README.md"),
            pyproject_head=PIN_ONLY_HEAD,
            pyproject_base=BASE_MANIFEST,
        )
        assert not ok

    def test_a_removed_non_pin_key_is_refused(self) -> None:
        """Deletion is a change. Comparing key UNIONS rather than head's keys
        is what catches it."""
        head = PIN_ONLY_HEAD.replace(
            '[project.scripts]\nomnimarket = "omnimarket.cli:main"\n\n', ""
        )
        ok, reason = classify_dependency_pin_only(
            PIN_ONLY_PATHS, pyproject_head=head, pyproject_base=BASE_MANIFEST
        )
        assert not ok
        assert "project.scripts" in reason
