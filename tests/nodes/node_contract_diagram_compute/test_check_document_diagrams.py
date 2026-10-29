# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""check_document_diagrams: the plan's fixture passes and each kind of drift fails with its reason."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_contract_diagram_compute.handlers.handler_check_document_diagrams import (
    HandlerCheckDocumentDiagrams,
)
from omnimarket.nodes.node_contract_diagram_compute.models.model_contract_diagram import (
    ModelCheckDocumentDiagramsRequest,
    ModelCheckDocumentDiagramsResult,
)
from tests.nodes.node_contract_diagram_compute.conftest import (
    diagram_blocks,
    fragment_blocks,
)

ANDON = "node_andon_stop_reducer:state_machine"


def check(text: str) -> ModelCheckDocumentDiagramsResult:
    return HandlerCheckDocumentDiagrams().handle(
        ModelCheckDocumentDiagramsRequest(document_text=text)
    )


def test_the_fixture_copy_of_the_plans_diagrams_passes(plan_text: str) -> None:
    result = check(plan_text)
    assert result.issues == ()
    assert result.exit_code == 0
    assert result.diagrams_checked == 11
    assert result.fragments_found == 10


def test_the_fixture_holds_seven_state_machines_and_three_graphs(
    plan_text: str,
) -> None:
    blocks = {key.rpartition(":")[2] for key in fragment_blocks(plan_text)}
    kinds = [block for _, block, _, _ in diagram_blocks(plan_text)]
    assert blocks == {"state_machine", "graph"}
    assert len(fragment_blocks(plan_text)) == 10
    assert sum(1 for kind in fragment_blocks(plan_text) if kind.endswith(":graph")) == 3
    assert kinds.count("state_machine") == 7
    assert (
        kinds.count("graph") == 4
    )  # the seam graph is drawn twice, full and collapsed


def test_a_state_diagram_with_one_edge_changed_fails_and_names_the_line(
    plan_text: str,
) -> None:
    old = "    fixed --> open : readback_mismatch\n"
    new = "    fixed --> stale : readback_mismatch\n"
    assert plan_text.count(old) == 1
    block = next(
        lines
        for node, kind, _, lines in diagram_blocks(plan_text)
        if f"{node}:{kind}" == ANDON
    )
    expected_line = block.index(old.rstrip("\n")) + 1

    result = check(plan_text.replace(old, new))

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["diagram_differs"]
    issue = result.issues[0]
    assert issue.contract_id == ANDON
    assert issue.diagram_line == expected_line
    assert old.strip() in issue.message
    assert new.strip() in issue.message
    assert f"line {expected_line}" in issue.message


def test_a_removed_transition_with_the_diagram_left_fails(plan_text: str) -> None:
    removed = (
        "  - version: {major: 1, minor: 0, patch: 0}\n"
        "    transition_name: fixed_to_open_on_readback_mismatch\n"
        "    from_state: fixed\n"
        "    to_state: open\n"
        "    trigger: readback_mismatch\n"
    )
    assert plan_text.count(removed) == 1

    result = check(plan_text.replace(removed, ""))

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["diagram_differs"]
    assert result.issues[0].contract_id == ANDON
    assert "fixed --> open : readback_mismatch" in result.issues[0].message


def test_a_diagram_with_no_fragment_fails(plan_text: str) -> None:
    fragment = fragment_blocks(plan_text)["node_lane_lease_reducer:state_machine"]
    marker = (
        "<!-- contract-fragment: node_lane_lease_reducer:state_machine -->\n```yaml\n"
    )
    assert plan_text.count(marker + fragment + "```\n") == 1

    result = check(plan_text.replace(marker + fragment + "```\n", ""))

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["diagram_without_fragment"]
    assert result.issues[0].contract_id == "node_lane_lease_reducer:state_machine"


def test_a_fragment_with_no_diagram_fails(plan_text: str) -> None:
    block = next(
        lines
        for node, kind, _, lines in diagram_blocks(plan_text)
        if f"{node}:{kind}" == "node_lane_lease_reducer:state_machine"
    )
    drawn = (
        "<!-- diagram-contract: node_lane_lease_reducer:state_machine -->\n```mermaid\n"
        + "\n".join(block)
        + "\n```\n"
    )
    assert plan_text.count(drawn) == 1

    result = check(plan_text.replace(drawn, ""))

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["fragment_without_diagram"]
    assert result.issues[0].contract_id == "node_lane_lease_reducer:state_machine"


def test_a_fragment_the_core_model_refuses_fails(plan_text: str) -> None:
    broken = plan_text.replace(
        "  initial_state: claimed\n", "  initial_state: no_such_state\n"
    )
    assert broken != plan_text

    result = check(broken)

    assert result.exit_code == 1
    kinds = {issue.kind for issue in result.issues}
    assert "fragment_refused" in kinds
    refused = next(i for i in result.issues if i.kind == "fragment_refused")
    assert refused.contract_id == "node_lane_lease_reducer:state_machine"


def test_a_diagram_with_no_line_naming_its_contract_fails(plan_text: str) -> None:
    caption = next(
        line
        for line in plan_text.splitlines()
        if line.startswith("*Figure 10.") and "node_lane_lease_reducer" in line
    )

    result = check(
        plan_text.replace(caption, "*Figure 10. The life of one lane lease.*")
    )

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["missing_contract_line"]
    assert result.issues[0].contract_id == "node_lane_lease_reducer:state_machine"


def test_two_fragments_with_one_id_fail(plan_text: str) -> None:
    fragment = fragment_blocks(plan_text)["node_lane_lease_reducer:state_machine"]
    marker = (
        "<!-- contract-fragment: node_lane_lease_reducer:state_machine -->\n```yaml\n"
    )
    doubled = plan_text + "\n" + marker + fragment + "```\n"

    result = check(doubled)

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["duplicate_fragment"]


def test_a_collapsed_view_of_a_state_machine_fails(plan_text: str) -> None:
    marked = plan_text.replace(
        "<!-- diagram-contract: node_lane_lease_reducer:state_machine -->",
        "<!-- diagram-contract: node_lane_lease_reducer:state_machine collapsed -->",
    )

    result = check(marked)

    assert [issue.kind for issue in result.issues] == ["view_not_supported"]


def test_a_marker_with_no_block_after_it_fails() -> None:
    result = check("<!-- diagram-contract: node_x:graph -->\nno block follows\n")

    assert result.exit_code == 1
    assert [issue.kind for issue in result.issues] == ["malformed_marker"]


def test_a_document_with_no_markers_passes_with_nothing_checked() -> None:
    result = check("# A document with no diagrams\n")

    assert result.exit_code == 0
    assert result.diagrams_checked == 0


@pytest.mark.parametrize("extra", ["", "\n", "\n\n"])
def test_blank_lines_before_the_fence_do_not_matter(plan_text: str, extra: str) -> None:
    marker = "<!-- diagram-contract: node_lane_lease_reducer:state_machine -->\n"
    assert check(plan_text.replace(marker, marker + extra)).exit_code == 0
