# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The eight operator capture and access mode test files stay cleared of the ledger file name.

Each file listed here used to name the ledger file or its path variable in an executable line. The census run over this tree must print no hit for any of
them, and the per-file ratchet table must hold no row for any of them, so a change that puts a reader back in one of
these files fails here as well as in the census gate. The positive control proves the census run
does see the hits of a file that still holds one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect import runtime_file_census

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RATCHET = (
    _REPO_ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect/ratchet.yaml"
)
_CLEARED = (
    "tests/nodes/node_operator_access_mode_effect/test_foreground_wait_guard.py",
    "tests/nodes/node_operator_access_mode_effect/test_operator_access_mode.py",
    "tests/nodes/node_operator_access_mode_effect/test_operator_hooks_refusal_row_omn21020.py",
    "tests/unit/nodes/node_operator_capture_compute/test_golden_chain_operator_capture.py",
    "tests/unit/nodes/node_operator_capture_effect/test_capture_accepted_append.py",
    "tests/unit/nodes/node_operator_capture_effect/test_operator_capture_effect.py",
    "tests/unit/nodes/node_operator_capture_effect/test_operator_capture_serve.py",
    "tests/unit/nodes/node_operator_capture_effect/test_words_promotion_effect.py",
)
# A file the census still lists today, so the run below is proven able to print a hit.
_STILL_LISTED = "src/omnimarket/handlers/ledger_row_grammar.py"


def _census_hit_paths(capsys: pytest.CaptureFixture[str]) -> tuple[int, set[str]]:
    code = runtime_file_census.main(
        [
            "--packaged-overlay",
            "--ratchet-file",
            str(_RATCHET),
            "--repos",
            "omnimarket",
            "--directory",
            f"omnimarket={_REPO_ROOT}",
            "--per-file",
            "--verbose",
        ],
        environ={"ONEX_LEDGER_CENSUS_ROOT": str(_REPO_ROOT)},
    )
    out = capsys.readouterr().out
    hit_paths = {
        line.split()[1].rsplit(":", 1)[0]
        for line in out.splitlines()
        if line.startswith(("  reader ", "  writer "))
    }
    return code, hit_paths


def test_census_prints_no_hit_for_a_cleared_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, hit_paths = _census_hit_paths(capsys)
    assert code == 0
    assert hit_paths.isdisjoint(_CLEARED), sorted(hit_paths & set(_CLEARED))


def test_census_run_sees_hits_of_a_file_that_still_holds_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, hit_paths = _census_hit_paths(capsys)
    assert code == 0
    assert _STILL_LISTED in hit_paths


def test_ratchet_table_holds_no_row_for_a_cleared_file() -> None:
    table = yaml.safe_load(_RATCHET.read_text(encoding="utf-8"))
    rows = table["files"]["omnimarket"]
    assert not set(rows) & set(_CLEARED), sorted(set(rows) & set(_CLEARED))
    assert _STILL_LISTED in rows
    assert table["ratchet"]["omnimarket"] == len(rows)
