# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whole-file reverse-substitution proof support for the placement migration.

The caller supplies the original files and the already enumerated edits. This
does not infer edits from the migrated files or accept an AST-equivalent rewrite.
Ruling 2026-11-04T19:35:02Z adds only formatting and PT018 assertion splits.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ENUMERATED_CLASSES = frozenset({"wiring", "assertion", "loader"})
MECHANICAL_EXCEPTIONS = frozenset(
    {"lint-required formatting", "PT018 assertion splits"}
)
MOVED_TEST_FILES = (
    "test_landing_placement.py",
    "test_landing_placement_burst.py",
    "test_landing_placement_load_bar.py",
    "test_landing_placement_mac_cpu_idle.py",
    "test_landing_placement_mac_memory_bar.py",
    "test_landing_placement_reserve.py",
)


@dataclass(frozen=True)
class EnumeratedChange:
    """One exact, pre-enumerated source block and its replacement."""

    kind: Literal["wiring", "assertion", "loader"]
    before: str
    after: str


def _ruff(source: str, *arguments: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            *arguments,
            "--stdin-filename",
            "test_moved.py",
            "-",
        ],
        input=source,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # Ruff check exits 1 for an unfixed PT018 diagnostic (for example an assert
    # with a message). It still returns the source; it does not authorize a fix.
    if result.returncode not in (0, 1) or (
        arguments[0] == "format" and result.returncode != 0
    ):
        raise ValueError(f"formatter refused source: {result.stderr}")
    return result.stdout


def _mechanical_form(source: str) -> str:
    # No general lint fixer: in particular, import sorting, assertion rewrites
    # from other rules, and removed comments/tests are not exception classes.
    split = _ruff(
        source, "check", "--isolated", "--select", "PT018", "--fix", "--unsafe-fixes"
    )
    return _ruff(split, "format", "--isolated", "--target-version", "py312")


def assert_reverse_substitution(
    original: str,
    moved: str,
    *,
    reverse_substitutions: tuple[tuple[str, str], ...] = (),
    enumerated_changes: tuple[EnumeratedChange, ...] = (),
) -> None:
    """Refuse any whole-file difference outside the enumerated edits and ruling.

    Reverse substitutions are literal fixture-to-source pairs. Enumerated edits
    are applied forward to the original, never discovered from the moved copy.
    Even deleted caller tests remain differences: S4c is a separate gap.
    """
    for fixture, source_value in reverse_substitutions:
        if not fixture or fixture == source_value:
            raise ValueError("reverse substitution must name distinct, nonempty values")
        if fixture not in moved:
            raise ValueError("reverse substitution does not occur in the moved file")
        moved = moved.replace(fixture, source_value)
    expected = original
    for change in enumerated_changes:
        if change.kind not in ENUMERATED_CLASSES:
            raise ValueError(f"unapproved enumerated class: {change.kind}")
        if not change.before or expected.count(change.before) != 1:
            raise ValueError("enumerated source block must occur exactly once")
        expected = expected.replace(change.before, change.after, 1)
    expected = _mechanical_form(expected)
    actual = _mechanical_form(moved)
    if expected != actual:
        difference = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile="original-with-enumerated-edits",
                tofile="reverse-substituted-moved",
            )
        )
        raise AssertionError(
            f"unapproved reverse-substitution difference:\n{difference}"
        )


def check_manifest() -> None:
    """Run the full AC2 proof; absent historical inputs refuse rather than pass.

    The manifest is a list of rows: filename, original_path,
    reverse_substitutions (pairs), and enumerated_changes (kind/before/after).
    Originals must be supplied separately from the migrated repository. CI's
    clone-free regression tests exercise the checker, not historical parity.
    """
    path = os.environ.get("LANDING_PLACEMENT_REVERSE_SUBSTITUTION_MANIFEST")
    if not path:
        raise ValueError("LANDING_PLACEMENT_REVERSE_SUBSTITUTION_MANIFEST is required")
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    names = [row["filename"] for row in rows]
    if sorted(names) != sorted(MOVED_TEST_FILES):
        raise ValueError("manifest must name every moved test exactly once")
    destination = Path(__file__).resolve().parent
    for row in rows:
        original = Path(row["original_path"]).resolve()
        moved = destination / row["filename"]
        if original == moved or destination in original.parents:
            raise ValueError("original must be independent of the moved test tree")
        assert_reverse_substitution(
            original.read_text(encoding="utf-8"),
            moved.read_text(encoding="utf-8"),
            reverse_substitutions=tuple(map(tuple, row["reverse_substitutions"])),
            enumerated_changes=tuple(
                EnumeratedChange(**change) for change in row["enumerated_changes"]
            ),
        )
