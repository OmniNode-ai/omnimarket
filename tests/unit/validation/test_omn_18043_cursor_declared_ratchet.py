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
        """One entry per exposure lacking a cursor column — never collapsed per table."""
        baseline = mod._read_baseline()
        assert len(baseline) == len(set(baseline)), "baseline contains duplicates"
        assert all("#" in entry for entry in baseline), (
            "every entry must carry an exposure index; a bare node::table key collides"
        )


_REAL_NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_CONSUMER_FLOW = "node_projection_consumer_flow"
_CONSUMER_FLOW_KEY = f"{_CONSUMER_FLOW}::consumer_flow_windows#0"
_CONSUMER_FLOW_CURSOR_LINE = '  cursor_column: "projection_cursor"\n'


def _run_main(mod, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["check_projection_cursor_declared.py", *args])
    return int(mod.main())


def _write_node(root: Path, name: str, contract: str) -> None:
    node = root / name
    node.mkdir()
    (node / "contract.yaml").write_text(contract)


class TestCursorColumnMustBeADeclaredColumn:
    """AC2: a declared cursor_column absent from ``columns`` is a HARD violation.

    The ratchet above tolerates a missing cursor because 55 exposures predate it.
    A cursor that names a column the exposure does not select is a different
    defect: the serving path reads ``row.get(cursor_column)`` off a row that never
    carries it. There is no legacy population to grandfather, so it is never
    baselined — not by an existing baseline, and not by ``--write-baseline``.
    """

    _ABSENT = (
        "projection_api:\n"
        "  expose: true\n"
        "  table: t\n"
        "  columns: [id, created_at]\n"
        "  cursor_column: projection_cursor\n"
    )

    def test_cursor_absent_from_columns_fails_the_gate(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        nodes = tmp_path / "nodes"
        nodes.mkdir()
        _write_node(nodes, "node_absent", self._ABSENT)
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("# empty baseline\n")
        monkeypatch.setattr(mod, "NODES_DIR", nodes)
        monkeypatch.setattr(mod, "BASELINE", baseline)

        assert _run_main(mod, monkeypatch) == 1
        err = capsys.readouterr().err
        assert "node_absent::t#0" in err, err
        assert "projection_cursor" in err, err

    def test_a_baseline_listing_the_exposure_does_not_excuse_it(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        nodes = tmp_path / "nodes"
        nodes.mkdir()
        _write_node(nodes, "node_absent", self._ABSENT)
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("node_absent::t#0\n")
        monkeypatch.setattr(mod, "NODES_DIR", nodes)
        monkeypatch.setattr(mod, "BASELINE", baseline)

        assert _run_main(mod, monkeypatch) == 1

    @pytest.mark.parametrize("extra", [(), ("--allow-growth",)])
    def test_write_baseline_refuses_to_record_it(
        self, mod, tmp_path, monkeypatch, extra
    ) -> None:
        nodes = tmp_path / "nodes"
        nodes.mkdir()
        _write_node(nodes, "node_absent", self._ABSENT)
        baseline = tmp_path / "baseline.txt"
        original = "# pre-existing baseline\n"
        baseline.write_text(original)
        monkeypatch.setattr(mod, "NODES_DIR", nodes)
        monkeypatch.setattr(mod, "BASELINE", baseline)

        assert _run_main(mod, monkeypatch, "--write-baseline", *extra) == 1
        assert baseline.read_text() == original, "the baseline must not be rewritten"

    def test_nested_exposure_absent_cursor_is_keyed_by_index(
        self, mod, tmp_path, monkeypatch
    ) -> None:
        nodes = tmp_path / "nodes"
        nodes.mkdir()
        _write_node(
            nodes,
            "node_nested",
            "projection_api:\n"
            "  expose: true\n"
            "  exposures:\n"
            "    - table: t\n"
            "      columns: [projection_cursor]\n"
            "      cursor_column: projection_cursor\n"
            "    - table: t\n"
            "      columns: [id]\n"
            "      cursor_column: projection_cursor\n",
        )
        monkeypatch.setattr(mod, "NODES_DIR", nodes)
        found = mod.membership_violations()
        assert [key for key, _ in found] == ["node_nested::t#1"], found

    @pytest.mark.parametrize(
        "columns",
        ["[id, projection_cursor]", "[id, '\"projection_cursor\"']", "['*']"],
        ids=["listed", "quoted", "select-star"],
    )
    def test_declared_and_listed_cursor_passes(
        self, mod, tmp_path, monkeypatch, columns
    ) -> None:
        nodes = tmp_path / "nodes"
        nodes.mkdir()
        _write_node(
            nodes,
            "node_ok",
            "projection_api:\n"
            "  expose: true\n"
            "  table: t\n"
            f"  columns: {columns}\n"
            "  cursor_column: projection_cursor\n",
        )
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("# empty baseline\n")
        monkeypatch.setattr(mod, "NODES_DIR", nodes)
        monkeypatch.setattr(mod, "BASELINE", baseline)

        assert _run_main(mod, monkeypatch) == 0


class TestTheRealTreeAgainstTheRegeneratedBaseline:
    def test_the_real_tree_passes_the_gate(self, mod, monkeypatch) -> None:
        assert _run_main(mod, monkeypatch) == 0
        assert mod.membership_violations() == []

    def test_consumer_flow_is_no_longer_baselined(self, mod) -> None:
        """It declares ``projection_cursor``; a stale entry would mask its removal."""
        assert _CONSUMER_FLOW_KEY not in mod._read_baseline()

    def test_removing_consumer_flows_cursor_fails_the_gate(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        """Mutate a tmp mirror of the tree, never the real contract."""
        mirror = tmp_path / "nodes"
        mirror.mkdir()
        for contract in sorted(_REAL_NODES_DIR.glob("node_*/contract.yaml")):
            (mirror / contract.parent.name).mkdir()
            target = mirror / contract.parent.name / "contract.yaml"
            if contract.parent.name == _CONSUMER_FLOW:
                text = contract.read_text()
                assert _CONSUMER_FLOW_CURSOR_LINE in text
                target.write_text(text.replace(_CONSUMER_FLOW_CURSOR_LINE, "", 1))
            else:
                target.symlink_to(contract)
        monkeypatch.setattr(mod, "NODES_DIR", mirror)

        assert _run_main(mod, monkeypatch) == 1
        assert _CONSUMER_FLOW_KEY in capsys.readouterr().err


# AC3: the 8 declarable exposures. Every other tracked exposure is the 55-entry
# measured gap recorded in scripts/validation/projection_cursor_baseline.txt.
#
# 7 -> 8 and 62 -> 63 for OMN-18768: node_projection_runner_fleet declares
# `cursor_column: projection_cursor` in its first commit, so it joins the
# DECLARED side and never enters the baseline. That is the direction this
# ratchet exists to produce -- the measured gap is unchanged at 55, because a
# new exposure that declares its cursor adds nothing to it. A new exposure
# that did NOT declare one would have to grow the baseline instead, in a diff
# a reviewer reads.
_AC3_DECLARABLE_EXPOSURES = frozenset(
    {
        "node_evidence_dashboard_reducer::evidence_dashboard_projection#0",
        "node_evidence_dashboard_reducer::evidence_correlation_trace_projection#1",
        "node_evidence_dashboard_reducer::evidence_readiness_aggregate_projection#2",
        "node_evidence_dashboard_reducer::evidence_correlation_trace_projection#3",
        "node_merge_state_projection::merge_state_transitions#0",
        "node_pr_merged_projection::pr_merged_events#0",
        "node_projection_runner_fleet::runner_fleet_liveness#0",
        # OMN-18769: the lab lane-health exposure declares `cursor_column: lane`
        # from its first commit. `lane` is the primary key, so it is unique and
        # stable -- which is what a cursor needs and what `projected_at` would
        # not be, since two lanes can be folded in the same instant. A new
        # exposure joins the DECLARED set; the measured gap below is the
        # never-declared backlog and does not grow.
        "node_projection_lab_lane_health::lab_lane_health#0",
        _CONSUMER_FLOW_KEY,
    }
)
# 64 as of OMN-18769: runner_fleet_liveness (OMN-18768) and lab_lane_health
# are two more tracked exposures. Both DECLARE a cursor_column from their
# first commit, so they join the declared set and _AC3_MEASURED_GAP -- the
# never-declared backlog -- is unmoved at 55.
_AC3_TOTAL_EXPOSURES = 64
_AC3_MEASURED_GAP = 55


class TestAc3DeclarableExposuresAndMeasuredGap:
    def test_seven_declare_a_member_cursor_and_the_baseline_holds_the_other_55(
        self, mod
    ) -> None:
        """Walk the real tree with the checker's own scan; no mirror, no mutation."""
        tracked = mod._tracked_exposures()
        declared = {
            exposure_id: exposure
            for exposure_id, exposure in tracked
            if exposure.get("cursor_column")
        }
        baseline = mod._read_baseline()

        assert len(tracked) == _AC3_TOTAL_EXPOSURES, sorted(i for i, _ in tracked)
        assert len(declared) == len(_AC3_DECLARABLE_EXPOSURES), sorted(declared)
        assert set(declared) == _AC3_DECLARABLE_EXPOSURES, sorted(declared)

        for exposure_id, exposure in declared.items():
            columns = exposure.get("columns")
            assert isinstance(columns, list), exposure_id
            assert str(exposure["cursor_column"]).strip('"') in {
                c.strip('"') for c in columns if isinstance(c, str)
            }, f"{exposure_id}: cursor not among declared columns {columns!r}"
            assert mod._cursor_membership_problem(exposure) is None, exposure_id

        assert len(baseline) == _AC3_MEASURED_GAP
        assert set(baseline) == {i for i, _ in tracked} - set(declared)
        assert len(declared) + len(baseline) == len(tracked)
