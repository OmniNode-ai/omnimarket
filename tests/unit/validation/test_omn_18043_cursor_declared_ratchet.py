# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18043 — the cursor_column ratchet must catch a NEW violation, not just count.

Two properties, and the second is the one that nearly shipped broken.

**It must see both contract shapes.** ``projection_api`` is written flat on some
contracts and as a nested ``exposures`` list on others — 21 each. A reader that
handles only the flat form reports the nested ones as declaring nothing, which
undercounts the fleet by 20 exposures. That mistake was made and withdrawn on
2026-09-08; this pins it.

**The exposure id must be unique per exposure, not per table.** Several nodes
expose the same table repeatedly: ``node_projection_delegation`` exposes
``delegation_events`` four times. Keyed on ``node::table`` alone, 5 of the 56
current violations collapse into shared entries — and a NEW violating exposure
added to an already-baselined table produces a key the baseline already
contains, so it lands silently. Measured directly: with the colliding key the
same mutation exits 0; with the indexed key it exits 1.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "validation"
    / "check_projection_cursor_declared.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_cursor_ratchet", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_cursor_ratchet"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class TestBothContractShapesAreRead:
    def test_flat_form_is_read(self, mod) -> None:
        contract = yaml.safe_load(
            "projection_api:\n  expose: true\n  table: some_table\n  columns: [a, b]\n"
        )
        assert len(mod._exposures(contract)) == 1

    def test_nested_exposures_form_is_read(self, mod) -> None:
        contract = yaml.safe_load(
            "projection_api:\n"
            "  expose: true\n"
            "  exposures:\n"
            "    - table: t1\n"
            "    - table: t2\n"
            "    - table: t3\n"
        )
        assert len(mod._exposures(contract)) == 3, (
            "the nested form must not read as 'declares nothing' — that undercounts "
            "the fleet by 20 exposures"
        )

    def test_expose_false_is_not_an_exposure(self, mod) -> None:
        assert (
            mod._exposures(yaml.safe_load("projection_api:\n  expose: false\n")) == []
        )

    def test_absent_section_is_not_an_exposure(self, mod) -> None:
        assert mod._exposures({"name": "node_x"}) == []


class TestExposureIdsAreUniquePerExposure:
    """The collision hole, pinned. A per-table key lets a new violation hide."""

    def test_repeated_tables_get_distinct_ids(self, mod, tmp_path, monkeypatch) -> None:
        node = tmp_path / "node_repeated"
        node.mkdir()
        (node / "contract.yaml").write_text(
            "projection_api:\n"
            "  expose: true\n"
            "  exposures:\n"
            "    - table: same_table\n"
            "    - table: same_table\n"
            "    - table: same_table\n"
        )
        monkeypatch.setattr(mod, "NODES_DIR", tmp_path)
        found = mod.violations()
        assert len(found) == 3, f"three exposures must yield three ids, got {found!r}"
        assert len(set(found)) == 3, f"ids must be distinct, got {found!r}"

    def test_a_declared_cursor_is_not_a_violation(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        node = tmp_path / "node_ok"
        node.mkdir()
        (node / "contract.yaml").write_text(
            "projection_api:\n"
            "  expose: true\n"
            "  exposures:\n"
            "    - table: t\n"
            "      cursor_column: projection_cursor\n"
            "    - table: t\n"
        )
        monkeypatch.setattr(mod, "NODES_DIR", tmp_path)
        found = mod.violations()
        assert len(found) == 1, (
            f"only the undeclared exposure is a violation: {found!r}"
        )


class TestTheCommittedBaselineIsHonest:
    def test_the_tree_is_green_against_its_own_baseline(self, mod) -> None:
        """The committed baseline must match the tree it was generated from."""
        current = set(mod.violations())
        baseline = set(mod._read_baseline())
        assert not (current - baseline), (
            f"unbaselined violations: {sorted(current - baseline)}"
        )

    def test_the_baseline_records_every_exposure_separately(self, mod) -> None:
        """56 exposures lack a cursor column, so 56 entries — not 51 collapsed ones."""
        baseline = mod._read_baseline()
        assert len(baseline) == len(set(baseline)), "baseline contains duplicates"
        assert all("#" in entry for entry in baseline), (
            "every entry must carry an exposure index; a bare node::table key collides"
        )
