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

import contextlib
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from omnimarket.models.contract_projection_check import (
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers import (
    check_cursor,
)
from omnimarket.nodes.node_contract_projection_check_effect.handlers.handler_contract_projection_gather import (
    HandlerContractProjectionGather,
)
from omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check import (
    main as runtime_main,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _CursorGate:
    """The cursor gate as the canonical node and its effect boundary expose it.

    OMN-20567 replaced ``scripts/validation/check_projection_cursor_declared.py``
    with ``node_contract_projection_check_compute`` (rule ``cursor``). This adapter
    keeps the original test bodies below unchanged: ``NODES_DIR`` and ``BASELINE``
    are retargetable per test exactly as the script's module globals were, and
    ``main`` runs the node's real runtime over that directory.
    """

    NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"
    BASELINE = _REPO_ROOT / "scripts" / "validation" / "projection_cursor_baseline.txt"

    _exposures = staticmethod(check_cursor._exposures)
    _cursor_membership_problem = staticmethod(check_cursor._membership_problem)

    @contextlib.contextmanager
    def _root(self) -> Iterator[Path]:
        """A repo-shaped root whose ``src/omnimarket/nodes`` is this gate's NODES_DIR."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "omnimarket").mkdir(parents=True)
            (root / "src" / "omnimarket" / "nodes").symlink_to(self.NODES_DIR)
            yield root

    def _gathered(self) -> list[ModelProjectionNodeSources]:
        with self._root() as root:
            return HandlerContractProjectionGather._nodes(
                root, "node_*/contract.yaml", with_modules=False
            )

    def _tracked_exposures(self) -> list[tuple[str, dict[str, object]]]:
        return check_cursor.tracked_exposures(self._gathered())[0]

    def violations(self) -> list[str]:
        return check_cursor.missing_cursor_ids(self._tracked_exposures())

    def membership_violations(self) -> list[tuple[str, str]]:
        return check_cursor.membership_violations(self._tracked_exposures())

    def _read_baseline(self) -> list[str]:
        return HandlerContractProjectionGather._baseline(self.BASELINE)

    def main(self) -> int:
        with self._root() as root:
            previous = Path.cwd()
            os.chdir(root)
            try:
                return runtime_main(
                    [
                        "--rule",
                        "cursor",
                        "--baseline",
                        str(self.BASELINE),
                        *sys.argv[1:],
                    ]
                )
            finally:
                os.chdir(previous)


def _load_module() -> _CursorGate:
    return _CursorGate()


@pytest.fixture
def mod() -> _CursorGate:
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
    monkeypatch.setattr(sys, "argv", ["projection-contract-check", *args])
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
#
# 8 -> 9 and 63 -> 64 for OMN-18770: node_projection_runtime_error_fingerprints
# likewise declares `cursor_column: projection_cursor` in its first commit, so
# it joins the DECLARED side and never enters the baseline. The measured gap
# is still unchanged at 55, for the same reason, which is the ratchet turning
# the right way twice in one window.
_AC3_DECLARABLE_EXPOSURES = frozenset(
    {
        "node_evidence_dashboard_reducer::evidence_dashboard_projection#0",
        "node_evidence_dashboard_reducer::evidence_correlation_trace_projection#1",
        "node_evidence_dashboard_reducer::evidence_readiness_aggregate_projection#2",
        "node_evidence_dashboard_reducer::evidence_correlation_trace_projection#3",
        "node_merge_state_projection::merge_state_transitions#0",
        "node_pr_merged_projection::pr_merged_events#0",
        "node_projection_runner_fleet::runner_fleet_liveness#0",
        "node_projection_topic_activity::topic_activity#0",
        "node_projection_usage_by_model_day::usage_by_model_day#0",
        # OMN-18769: the lab lane-health exposure declares `cursor_column: lane`
        # from its first commit. `lane` is the primary key, so it is unique and
        # stable -- which is what a cursor needs and what `projected_at` would
        # not be, since two lanes can be folded in the same instant. A new
        # exposure joins the DECLARED set; the measured gap below is the
        # never-declared backlog and does not grow.
        "node_projection_lab_lane_health::lab_lane_health#0",
        _CONSUMER_FLOW_KEY,
        "node_projection_runtime_error_fingerprints::runtime_error_fingerprints#0",
        # OMN-18999: the prod-promotion-gate exposure declares
        # `cursor_column: projection_cursor` from its first commit. The
        # column is a BIGSERIAL on the relation's create migration, so it is
        # unique, monotonic and database-assigned -- which is what a cursor
        # needs and what `projected_at` would not be, since two decisions can
        # be folded in the same instant. The measured gap below does not grow.
        "node_projection_prod_promotion_gate::prod_promotion_gate_decisions#0",
        # OMN-19937: database-assigned BIGSERIAL cursor, declared with the
        # board probe-results exposure from its first commit.
        "node_projection_board_probe_results::board_probe_results#0",
        # OMN-19793: delegation_eval_results declares `cursor_column:
        # projection_cursor` from its first commit.
        "node_projection_delegation_eval::delegation_eval_results#0",
    }
)
# 65 as of OMN-18769: runner_fleet_liveness (OMN-18768) and lab_lane_health
# are two more tracked exposures. Both DECLARE a cursor_column from their
# first commit, so they join the declared set and _AC3_MEASURED_GAP -- the
# never-declared backlog -- is unmoved at 55.
# 66 as of OMN-18999: prod_promotion_gate_decisions is one more tracked
# exposure. It DECLARES a cursor_column from its first commit -- the column
# is on the relation's create migration -- so it joins the declared set and
# the never-declared backlog is again unmoved at 55.
# 67 as of OMN-19716: topic_activity declares its BIGSERIAL cursor in the
# same change as the exposure, so the measured backlog remains unchanged.
# 68 as of OMN-19937: board_probe_results does the same.
# 69 as of OMN-19978: usage_by_model_day declares its BIGSERIAL cursor in the
# same change as the exposure, so the measured backlog remains unchanged.
# 70 as of OMN-19793: delegation_eval_results does the same.
_AC3_TOTAL_EXPOSURES = 70
_AC3_MEASURED_GAP = 55


class TestAc3DeclarableExposuresAndMeasuredGap:
    def test_eight_declare_a_member_cursor_and_the_baseline_holds_the_other_55(
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
