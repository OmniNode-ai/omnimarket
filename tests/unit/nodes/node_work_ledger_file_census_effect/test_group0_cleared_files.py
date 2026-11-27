# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The group 0 files that moved off the ledger file name stay cleared.

Nine src files moved their ledger reads to the bus, and seven row grammar tests stopped
spelling the ledger file name (they take the governed name from the grammar and build any path
under the temp dir). Each file listed here used to hold an executable reader of the work ledger
file or its path variable. The census run over this tree must print no hit for any of them, and the per-file
ratchet table must hold no row for any of them, so a change that puts a reader back in one of
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
    "src/omnimarket/handlers/scm/scm_duplicate_guard.py",
    "src/omnimarket/handlers/wip_admission_brief_gate.py",
    "src/omnimarket/nodes/node_ask_state_effect/contract.yaml",
    "src/omnimarket/nodes/node_ask_state_effect/runtime_ask_state.py",
    "src/omnimarket/nodes/node_bus_binding_reducer/contract.yaml",
    "src/omnimarket/nodes/node_context_source_repo_doctrine_effect/contract.yaml",
    "src/omnimarket/nodes/node_dev_runtime_mutation_guard_effect/contract.yaml",
    "src/omnimarket/nodes/node_entrypoint_lib_compute/handlers/handler_entrypoint_lib.py",
    "src/omnimarket/nodes/node_exception_queue_effect/container/container_exception_queue_effect.py",
    "tests/handlers/ledger_row_grammar/test_ledger_claim_contract_omn20029.py",
    "tests/handlers/ledger_row_grammar/test_ledger_grammar_prompt_link_omn18531.py",
    "tests/handlers/ledger_row_grammar/test_ledger_hold_grammar.py",
    "tests/handlers/ledger_row_grammar/test_ledger_ruling_link_omn17427.py",
    "tests/handlers/ledger_row_grammar/test_ledger_scoped_release.py",
    "tests/handlers/ledger_row_grammar/test_ledger_terminal_delegation_cell_omn17427.py",
    "tests/handlers/ledger_row_grammar/test_ledger_worktree_cell.py",
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
