# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A code lane closes partial without an exemplar and graph cell (OMN-24686).

Golden chain: a lane whose brief names a repo and carries an EXEMPLAR and a GRAPH step (the brief check's own definition of a code
lane, read here as the steps it requires) closes with the outcome it declared when its TERMINAL carries
``exemplar=<path>`` and ``graph=<query -> answer>``; ``graph=unreachable:<why>`` is accepted and kept as a finding. Error chain: a code
lane with either cell missing, a placeholder or malformed closes ``partial`` with the problem ``no exemplar or graph cell``. The
no-code case is unchanged: a brief that writes no code, a brief naming no repo and a request with no brief close as they did.
The exemplar for the rule is the ``no DELEGATION line in the final message`` problem of the same handler.
"""

from __future__ import annotations

import pytest

from omnimarket.models.remote_lane_close import (
    ModelRemoteLaneResult,
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_remote_lane_close_compute.handlers.handler_remote_lane_result import (
    HandlerRemoteLaneResult,
)

pytestmark = pytest.mark.unit

PROBLEM = "no exemplar or graph cell"
DELEGATION = "DELEGATION delegated=0 reason=no-text-or-code"
CLAIM = (
    '```bash\npython3 onex_ledger.py "$L" --append "$(date -u) | CLAIM | lane=l-0001 | actor=a | model=sonnet | '
    "ticket=OMN-1 | parent=p | band=B3 | repo={repo} | worktree=omni_worktrees/OMN-1/{repo} | scope=s | "
    'est ~1 lane-hours; displaces x; (OMN-1)"\n```\n'
)
STANDING_RULES = (
    "# Standing rules for every dispatched lane (read fully before the first tool call)\n\n"
    "Tests: TDD, reproduce RED first. Write the code and add a test file for every handler you touch.\n\n"
)


def brief(task: str, repo: str = "omnimarket") -> str:
    return (
        f"# Lane l-0001: a task\n\n## 2. Task\n\n{task}\n\n## 3. Rules\n\n{STANDING_RULES}"
        f"## 3a. Constraints (pasted, never paraphrased)\n\n### 3a.8 CLAIM template\n\n{CLAIM.format(repo=repo)}"
    )


STEPS = (
    "EXEMPLAR: find and name the closest existing node and match its shape.\n"
    "GRAPH: query the code graph for importers and definitions of the symbols you change.\n"
)
CODE_BRIEF = brief(
    "Add a handler to the close node and write its test, then open a draft pull request.\n"
    + STEPS
)
SECTION_BRIEF = brief(
    "Add a handler.\n\n## 1. EXEMPLAR\n\nThe closest node is node_x; match its shape.\n\n"
    "## 2. GRAPH\n\nImporters and definitions of HandlerX from the code graph.\n\n## 4. Report\n\nstop\n"
)
NO_CODE_BRIEF = brief(
    "Read the ledger rows of the last day and write a report on lane throughput. This lane writes no code."
)
NO_REPO_BRIEF = brief(
    "Add a handler to the close node and write its test.\n" + STEPS, repo="none"
)
OPEN_ENDED_BRIEF = brief("Lane kind: open-ended\n\nDiagnose the cause.\n" + STEPS)


def message(*lines: str) -> str:
    return "\n".join([*lines, DELEGATION])


def close(
    final_message: str | None,
    lane_brief: str = CODE_BRIEF,
    exit_code: int = 0,
) -> ModelRemoteLaneResult:
    return HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(
            engine_exit_code=exit_code, final_message=final_message, brief=lane_brief
        )
    )


def test_exemplar_graph_cell_golden_a_code_lane_with_both_cells_keeps_its_outcome() -> (
    None
):
    result = close(
        message(
            "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/pkg/nodes/node_x/handlers/handler_x.py | "
            "graph=importers of HandlerX -> 2 files | friction=none"
        )
    )
    assert (result.outcome, result.problem, result.findings) == ("handed-off", None, ())


def test_exemplar_graph_cell_golden_cells_on_a_lane_result_line_count() -> None:
    result = close(
        message(
            "LANE_RESULT outcome=done exemplar=src/a/b.py graph=definitions of foo -> src/a/b.py:12"
        )
    )
    assert (result.outcome, result.problem) == ("done", None)


def test_exemplar_graph_cell_golden_unreachable_graph_is_accepted_and_recorded_as_a_finding() -> (
    None
):
    result = close(
        message(
            "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | "
            "graph=unreachable:ARCH_GRAPH_BOLT_URI unset on the host | friction=none"
        )
    )
    assert (result.outcome, result.problem) == ("handed-off", None)
    assert result.findings == (
        "graph unreachable: ARCH_GRAPH_BOLT_URI unset on the host",
    )


@pytest.mark.parametrize(
    "terminal",
    [
        "TERMINAL | lane=l-0001 | outcome=handed-off | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | graph=importers of X -> 2 files | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=<path> | graph=q -> a | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=none | graph=q -> a | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=none: | graph=q -> a | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=none:tbd | graph=q -> a | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | graph=importers of X | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | graph=unreachable: | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | graph=unreachable:tbd | friction=none",
        "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | graph= -> | friction=none",
    ],
)
def test_exemplar_graph_cell_error_a_code_lane_without_a_real_cell_closes_partial(
    terminal: str,
) -> None:
    result = close(message(terminal))
    assert (result.outcome, result.problem) == ("partial", PROBLEM)
    assert result.declared_outcome == "handed-off"


def test_exemplar_graph_cell_error_a_cell_quoted_in_a_fence_is_not_a_cell() -> None:
    result = close(
        message(
            "```",
            "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | graph=q -> a",
            "```",
            "LANE_RESULT outcome=handed-off",
        )
    )
    assert (result.outcome, result.problem) == ("partial", PROBLEM)


@pytest.mark.parametrize("declared", ["done", "handed-off"])
def test_exemplar_graph_cell_error_both_finishing_outcomes_are_held_back(
    declared: str,
) -> None:
    assert close(message(f"LANE_RESULT outcome={declared}")).outcome == "partial"


@pytest.mark.parametrize("declared", ["blocked", "partial", "skipped-owned", "unknown"])
def test_exemplar_graph_cell_error_an_outcome_that_claims_no_finish_is_left_alone(
    declared: str,
) -> None:
    result = close(message(f"LANE_RESULT outcome={declared}"))
    assert (result.outcome, result.problem) == (declared, None)


def test_exemplar_graph_cell_error_an_engine_failure_and_a_missing_delegation_line_still_win() -> (
    None
):
    failed = close(message("LANE_RESULT outcome=done"), exit_code=1)
    assert (failed.outcome, failed.problem) == ("failed", None)
    rejected = close("LANE_RESULT outcome=done")
    assert rejected.outcome == "rejected-no-delegation"
    assert rejected.problem == "no DELEGATION line in the final message"


@pytest.mark.parametrize(
    "lane_brief",
    [NO_CODE_BRIEF, NO_REPO_BRIEF, OPEN_ENDED_BRIEF, ""],
    ids=["no-code", "no-repo", "open-ended", "no-brief"],
)
def test_exemplar_graph_cell_no_code_lane_is_not_refused(lane_brief: str) -> None:
    result = close(message("LANE_RESULT outcome=handed-off"), lane_brief)
    assert (result.outcome, result.problem) == ("handed-off", None)


# --- OMN-24731: the review's blocking findings -------------------------------------------------------------------------------


def test_exemplar_graph_cell_ledger_cells_carry_both_cells_to_the_closing_row() -> None:
    result = close(
        message(
            "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=src/a/b.py | "
            "graph=importers of X -> 2 files | friction=none"
        )
    )
    assert result.ledger_cells["exemplar"] == "src/a/b.py"
    assert result.ledger_cells["graph"] == "importers of X -> 2 files"


def test_exemplar_graph_cell_ledger_cells_are_one_cell_without_a_bar() -> None:
    report = '{"outcome": "done", "exemplar": "src/a/b.py", "graph": "q | x ->\\n a"}'
    result = close(message(report))
    assert result.ledger_cells["graph"] == "q / x -> a"


def test_exemplar_graph_cell_no_ledger_cells_when_the_cells_are_not_real() -> None:
    result = close(message("LANE_RESULT outcome=done exemplar=<path> graph=q -> a"))
    assert "exemplar" not in result.ledger_cells
    assert "graph" not in result.ledger_cells


def test_exemplar_graph_cell_exemplar_none_with_a_reason_is_accepted_and_kept_as_a_finding() -> (
    None
):
    result = close(
        message(
            "TERMINAL | lane=l-0001 | outcome=handed-off | exemplar=none:no node of this kind exists | "
            "graph=importers of X -> 2 files | friction=none"
        )
    )
    assert (result.outcome, result.problem) == ("handed-off", None)
    assert result.findings == ("exemplar none: no node of this kind exists",)
    assert result.ledger_cells["exemplar"] == "none:no node of this kind exists"


def test_exemplar_graph_cell_both_absences_are_two_findings() -> None:
    result = close(
        message(
            "LANE_RESULT outcome=done exemplar=none:first of its kind graph=unreachable:bolt down"
        )
    )
    assert result.outcome == "done"
    assert result.findings == (
        "exemplar none: first of its kind",
        "graph unreachable: bolt down",
    )


def test_exemplar_graph_cell_one_definition_a_brief_with_no_step_is_not_judged_whatever_its_words() -> (
    None
):
    """The brief check refused or passed it; this node holds no word list of its own."""
    lane_brief = brief(
        "Add a handler to the close node and write its test, then open a draft pull request."
    )
    result = close(message("LANE_RESULT outcome=handed-off"), lane_brief)
    assert (result.outcome, result.problem) == ("handed-off", None)


def test_exemplar_graph_cell_one_definition_a_brief_carrying_a_step_is_judged_whatever_its_words() -> (
    None
):
    lane_brief = brief(
        "Collect the numbers into a table.\nEXEMPLAR: the closest throughput report.\n"
    )
    result = close(message("LANE_RESULT outcome=handed-off"), lane_brief)
    assert (result.outcome, result.problem) == ("partial", PROBLEM)


def test_exemplar_graph_cell_a_template_or_fenced_step_does_not_make_a_code_lane() -> (
    None
):
    lane_brief = brief(
        "Write a report.\nEXEMPLAR: <path of the closest node>\n```\nGRAPH: importers and definitions\n```\n"
    )
    result = close(message("LANE_RESULT outcome=handed-off"), lane_brief)
    assert (result.outcome, result.problem) == ("handed-off", None)


def test_exemplar_graph_cell_sections_count_as_steps() -> None:
    result = close(message("LANE_RESULT outcome=handed-off"), SECTION_BRIEF)
    assert (result.outcome, result.problem) == ("partial", PROBLEM)
    ok = close(
        message("LANE_RESULT outcome=handed-off exemplar=src/a/b.py graph=q -> a"),
        SECTION_BRIEF,
    )
    assert (ok.outcome, ok.problem) == ("handed-off", None)


def test_exemplar_graph_cell_an_empty_section_is_not_a_step() -> None:
    lane_brief = brief(
        "Add a handler.\n\n## 1. EXEMPLAR\n\n## 2. GRAPH\n\n## 4. Report\n\nx\n"
    )
    result = close(message("LANE_RESULT outcome=handed-off"), lane_brief)
    assert (result.outcome, result.problem) == ("handed-off", None)


def test_exemplar_graph_cell_a_json_report_carries_the_cells() -> None:
    report = '{"outcome": "handed-off", "exemplar": "src/a/b.py", "graph": "importers of X -> 2 files"}'
    result = close(message(report))
    assert (result.outcome, result.problem) == ("handed-off", None)
    assert result.ledger_cells["graph"] == "importers of X -> 2 files"


def test_exemplar_graph_cell_lines_after_the_delegation_line_are_not_read() -> None:
    result = close(
        "LANE_RESULT outcome=handed-off\n" + DELEGATION + "\n"
        "TERMINAL | exemplar=src/a/b.py | graph=q -> a"
    )
    assert (result.outcome, result.problem) == ("partial", PROBLEM)
