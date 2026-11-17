# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a fixer push that adds files is held until the comment names the finding for each."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_review_lock_effect.handlers.handler_scope_check import (
    HandlerScopeCheck,
    contract_paths,
)
from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    EnumScopeVerdict,
    ModelScopeCheckRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = tuple((FIXTURES / "pr6047_files_7ce37985d13c.txt").read_text().split())
LAST = tuple((FIXTURES / "pr6047_files_fca8c3ea6891.txt").read_text().split())
CONTRACT = FIXTURES / "pr6047_contract_OMN-24903.yaml"

ADDED = (
    "contracts/OMN-25313.yaml",
    "contracts/OMN-25339.yaml",
    "contracts/OMN-25367.yaml",
    "docs/env-example-full.txt",
    "scripts/deploy-agent/deploy_agent/executor.py",
    "scripts/deploy-agent/tests/unit/test_gateway_lane_scope_omn18134.py",
)

# A fixer comment written for this fixture: each bullet names a file and the finding that asked for it.
NAMING_ALL = """\
Second fix round, review of head 0b0a4b819f86. Files added in this push:
- contracts/OMN-25313.yaml: finding F1, the contract for the roll order fix
- contracts/OMN-25339.yaml: finding F2 asked for a contract on the writers roll
- contracts/OMN-25367.yaml: finding F3, revision label guard contract
- docs/env-example-full.txt: finding F4, the new overlay variable is documented
- scripts/deploy-agent/deploy_agent/executor.py: finding F2, recreate after the deps leg
- test_gateway_lane_scope_omn18134.py: finding F5, the gateway scope assertion
"""


def _request(comment: str, **extra: object) -> ModelScopeCheckRequest:
    return ModelScopeCheckRequest(
        first_files=FIRST, new_files=LAST, comment=comment, **extra
    )  # type: ignore[arg-type]


def test_fixtures_are_the_6_and_12_file_heads() -> None:
    assert (len(FIRST), len(LAST)) == (6, 12)
    assert set(FIRST) <= set(LAST)


def test_6047_push_is_held_with_the_added_files_when_no_finding_is_named() -> None:
    result = HandlerScopeCheck().handle(
        _request("Fixed the review comments, please re-review.")
    )
    assert result.verdict is EnumScopeVerdict.HOLD
    assert result.exit_code == 3
    assert result.added_files == ADDED
    assert result.unnamed_files == ADDED
    assert result.scope_source == "first-head-file-set"


def test_6047_push_is_released_once_the_finding_is_named_for_each_added_file() -> None:
    result = HandlerScopeCheck().handle(_request(NAMING_ALL))
    assert result.verdict is EnumScopeVerdict.RELEASE
    assert result.exit_code == 0
    assert result.unnamed_files == ()
    assert set(result.named_files) == set(ADDED)
    assert result.named_files["scripts/deploy-agent/deploy_agent/executor.py"] == "F2"


def test_one_unnamed_file_still_holds_and_is_the_only_one_listed() -> None:
    comment = "\n".join(
        line for line in NAMING_ALL.splitlines() if "env-example" not in line
    )
    result = HandlerScopeCheck().handle(_request(comment))
    assert result.verdict is EnumScopeVerdict.HOLD
    assert result.unnamed_files == ("docs/env-example-full.txt",)


def test_a_finding_without_the_file_and_a_file_without_a_finding_do_not_release() -> (
    None
):
    comment = (
        "Findings F1 F2 F3 F4 F5 are all addressed.\n"
        "Added contracts/OMN-25313.yaml contracts/OMN-25339.yaml contracts/OMN-25367.yaml"
        " docs/env-example-full.txt executor.py test_gateway_lane_scope_omn18134.py.\n"
    )
    result = HandlerScopeCheck().handle(_request(comment))
    assert result.verdict is EnumScopeVerdict.HOLD
    assert result.unnamed_files == ADDED


def test_indented_continuation_line_belongs_to_its_bullet() -> None:
    comment = "- docs/env-example-full.txt\n    asked for by finding F4\n"
    result = HandlerScopeCheck().handle(
        ModelScopeCheckRequest(
            first_files=("a.py",),
            new_files=("a.py", "docs/env-example-full.txt"),
            comment=comment,
        )
    )
    assert result.verdict is EnumScopeVerdict.RELEASE


def test_a_push_adding_nothing_is_released_and_a_removed_file_is_not_growth() -> None:
    result = HandlerScopeCheck().handle(
        ModelScopeCheckRequest(first_files=("a.py", "b.py"), new_files=("a.py",))
    )
    assert result.verdict is EnumScopeVerdict.RELEASE
    assert result.added_files == ()


def test_review_comment_id_counts_as_a_finding() -> None:
    comment = "- docs/x.md: the ask in review comment 6948601820\n"
    result = HandlerScopeCheck().handle(
        ModelScopeCheckRequest(
            first_files=("a.py",), new_files=("a.py", "docs/x.md"), comment=comment
        )
    )
    assert result.verdict is EnumScopeVerdict.RELEASE


def test_custom_finding_pattern() -> None:
    result = HandlerScopeCheck().handle(
        ModelScopeCheckRequest(
            first_files=("a.py",),
            new_files=("a.py", "b.py"),
            comment="b.py is for REV-12",
            finding_pattern=r"REV-\d+",
        )
    )
    assert result.verdict is EnumScopeVerdict.RELEASE


def test_contract_paths_are_read_from_the_contract_text() -> None:
    paths = contract_paths(CONTRACT.read_text())
    assert (
        "src/omnibase_infra/nodes/node_setup_local_provision_effect/runtime_cost_estimator_build.py"
        in paths
    )
    assert (
        "scripts/deploy-agent/tests/unit/test_dev_lane_only_scope_omn18134.py"
        not in paths
    )


def test_contract_scope_is_used_and_named_when_passed() -> None:
    text = CONTRACT.read_text()
    contract_paths_found = contract_paths(text)
    assert (
        "scripts/deploy-agent/tests/unit/test_stability_writers_follow_runtime_omn24903.py"
        in contract_paths_found
    )
    result = HandlerScopeCheck().handle(
        _request("", contract_path="contracts/OMN-24903.yaml", contract_text=text)
    )
    assert result.scope_source == "contract:contracts/OMN-24903.yaml"
    assert result.verdict is EnumScopeVerdict.HOLD
    # A file the contract names is not growth even when the first head did not hold it.
    named_by_contract = ModelScopeCheckRequest(
        first_files=("a.py",),
        new_files=(
            "a.py",
            "src/omnibase_infra/nodes/node_setup_local_provision_effect/runtime_cost_estimator_build.py",
        ),
        contract_path="contracts/OMN-24903.yaml",
        contract_text=text,
    )
    assert (
        HandlerScopeCheck().handle(named_by_contract).verdict
        is EnumScopeVerdict.RELEASE
    )


def test_contract_naming_no_path_falls_back_to_the_first_head_and_says_so() -> None:
    result = HandlerScopeCheck().handle(
        _request(
            "",
            contract_path="contracts/OMN-1.yaml",
            contract_text="title: nothing here\n",
        )
    )
    assert result.scope_source == "first-head-file-set"
    assert "names no path" in result.detail
    assert result.verdict is EnumScopeVerdict.HOLD
