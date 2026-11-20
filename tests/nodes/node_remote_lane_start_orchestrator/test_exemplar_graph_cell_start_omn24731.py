# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start orchestrator sends the lane brief to the close node, so a code lane without cells closes partial (OMN-24731).

Review finding 2 of the exemplar and graph close rule: the rule needs ``request.brief`` and the orchestrator built the close
request without it, so no lane was ever marked partial. End to end: the brief a lane was started with, its final message, the
TERMINAL row the orchestrator writes, judged by the ledger's own row grammar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    cells,
    request,
    stream_result,
)

pytestmark = [pytest.mark.unit]

DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
CODE_BRIEF = (
    "# Lane floor-alpha-a1\n\n## 2. Task\n\nAdd a handler and write its test.\n"
    "EXEMPLAR: find and name the closest existing node and match its shape.\n"
    "GRAPH: query the code graph for importers and definitions of the symbols you change.\n\n"
    "## 3a. Constraints (pasted)\n\n### 3a.8 CLAIM template\n\n```bash\n"
    'append "$(date) | CLAIM | lane=floor-alpha-a1 | repo=omnimarket | scope=s"\n```\n'
)


def _terminal(tmp_path: Path, message: str, brief: str) -> tuple[Rig, str]:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(exit_polls=1, log_lines=(stream_result(message),)),
    )
    rig.orchestrator.run(request(brief=brief))
    [terminal] = rig.ledger_rows("TERMINAL")
    return rig, terminal


def test_exemplar_graph_cell_start_a_code_lane_with_no_cells_closes_partial(
    tmp_path: Path,
) -> None:
    rig, terminal = _terminal(
        tmp_path, f"LANE_RESULT outcome=handed-off\n{DELEGATION}", CODE_BRIEF
    )
    assert cells(terminal)["outcome"] == "partial"
    assert "no exemplar or graph cell" in terminal
    assert all(not grammar.row_refusals(row) for row in rig.executor.attempts)


def test_exemplar_graph_cell_start_the_cells_ride_to_the_closing_row(
    tmp_path: Path,
) -> None:
    rig, terminal = _terminal(
        tmp_path,
        "LANE_RESULT outcome=handed-off exemplar=src/a/b.py graph=importers of X -> 2 files\n"
        + DELEGATION,
        CODE_BRIEF,
    )
    found = cells(terminal)
    assert found["outcome"] == "handed-off"
    assert found["exemplar"] == "src/a/b.py"
    assert found["graph"] == "importers of X -> 2 files"
    assert all(not grammar.row_refusals(row) for row in rig.executor.attempts), [
        grammar.row_refusals(row) for row in rig.executor.attempts
    ]


def test_exemplar_graph_cell_start_a_lane_that_writes_no_code_closes_as_declared(
    tmp_path: Path,
) -> None:
    _, terminal = _terminal(
        tmp_path,
        f"LANE_RESULT outcome=handed-off\n{DELEGATION}",
        "print the host name and stop",
    )
    assert cells(terminal)["outcome"] == "handed-off"
    assert "exemplar" not in cells(terminal)
