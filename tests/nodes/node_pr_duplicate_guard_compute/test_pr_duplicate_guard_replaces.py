# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ``replaces`` entry the node read as closed and not merged owns its lines (OMN-19998).

The measured case: branch ``omn-19998-glm-lane-route`` rewrites the same two hunks as open pull
request 1643 of ``example_repo`` (and 1643 changes a third file of its own, so it is a
real bundle), so without ``replaces`` the guard refuses with a LINES evidence. But the blame of
both hunks names only commit ``9e88a3986000`` - the whole change of pull request 1574, which
the node read as CLOSED and not merged. Naming 1574 in ``replaces`` sets those two overlaps
aside: the guard answers ``clear`` and lists in ``set_aside`` each overlap it did not count.
An overlap whose lines any other commit also introduced still counts, an overlap nobody can
attribute is never set aside, and a ``replaces`` entry the node cannot read as a closed,
unmerged pull request of this repository is refused with a typed reason. Without a
``replaces`` entry, blame records on the request change today's answer not at all.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    EnumReplacesRefusal,
    ModelFileTouch,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
    ModelPrTouch,
    ModelReplacedPrFacts,
    ModelSpanCommits,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
PACKAGE = "omnimarket.nodes.node_pr_duplicate_guard_compute"
REPO = "OmniNode-ai/example_repo"
SKILL = "plugins/omni/skills/lane-brief/SKILL.md"
FAILOVER = (
    "src/omni/nodes/node_lane_placement_recovery_effect"
    "/handlers/handler_lane_engine_failover.py"
)
OTHER = "src/omni/other.py"  # only 1643 changes it, so 1643 is a real bundle
#: The commit whose blame alone covers both shared hunks: all of replaced 1574.
CLOSED_COMMIT = "9e88a3986000"


def touch(*files: ModelFileTouch) -> ModelPrTouch:
    return ModelPrTouch(files=files)


def edit(
    path: str,
    *spans: tuple[int, int],
    digest: tuple[str, ...] = (),
    attributed: tuple[ModelSpanCommits, ...] = (),
) -> ModelFileTouch:
    return ModelFileTouch(path=path, spans=spans, digest=digest, attributed=attributed)


def blame(start: int, *commits: str) -> ModelSpanCommits:
    """The commits that introduced the hunk at one pre-image line; none: unattributable."""
    return ModelSpanCommits(start=start, end=start, commits=commits)


def retention(skill: ModelSpanCommits, failover: ModelSpanCommits) -> ModelOpenPr:
    """Open pull request 1643: the same two hunks as the new branch, plus a file of its own."""
    return ModelOpenPr(
        number=1643,
        head_ref="omn-19998-retention-stdin",
        base="main",
        title="fix(remote-lane): stream retention over stdin (OMN-19998)",
        touch=touch(
            edit(SKILL, (285, 285), digest=("aaa111aaa111",), attributed=(skill,)),
            edit(
                FAILOVER, (163, 163), digest=("bbb222bbb222",), attributed=(failover,)
            ),
            edit(OTHER, (700, 700)),
        ),
    )


RETENTION = retention(blame(285, CLOSED_COMMIT), blame(163, CLOSED_COMMIT))
CLOSED_1574 = ModelReplacedPrFacts(
    repo=REPO,
    number=1574,
    state="CLOSED",
    merged=False,
    commits=(CLOSED_COMMIT,),
)


def run(**fields: object) -> ModelPrDuplicateGuardResult:
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate({"now": NOW, **fields})
    )


def measured(**fields: object) -> ModelPrDuplicateGuardResult:
    """The GLM lane route folded against open 1643; ``fields`` add or override."""
    return run(
        **{
            "repo": REPO,
            "head_ref": "omn-19998-glm-lane-route",
            "base": "main",
            "title": "feat(OMN-19998): GLM lane route and call limits",
            "touch": touch(
                edit(SKILL, (285, 285), digest=("aaa111aaa111",)),
                edit(FAILOVER, (163, 163), digest=("bbb222bbb222",)),
            ),
            "open_prs": (RETENTION,),
            **fields,
        }
    )


# ---- the measured case: a closed, unmerged change owns the two shared hunks


def test_without_a_replaces_entry_the_shared_lines_still_refuse() -> None:
    """Control: no replaces named, so the guard adopts 1643 and sets nothing aside."""
    result = measured()

    assert result.state == "adopt"
    assert result.adopt_pr == "example_repo#1643"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES
    assert result.set_aside == ()


def test_the_closed_replaced_change_owns_both_overlaps_and_the_guard_clears() -> None:
    """Both overlaps were introduced only by closed 1574, so the guard answers clear."""
    result = measured(replaced=(CLOSED_1574,))

    assert result.state == "clear"
    assert len(result.set_aside) == 2
    for item in result.set_aside:
        assert item.pr == "example_repo#1643"
        assert item.commits == (CLOSED_COMMIT,)
        assert item.replaced == ("OmniNode-ai/example_repo#1574",)
    assert [item.place.rsplit("/", 1)[-1] for item in result.set_aside] == [
        "SKILL.md:285",
        "handler_lane_engine_failover.py:163",
    ]
    assert "Set aside 2 overlaps" in result.message


def test_an_overlap_another_commit_also_introduced_still_counts() -> None:
    """Line 163's blame holds a commit 1574 does not own, so only line 285 is set aside."""
    result = measured(
        open_prs=(
            retention(
                blame(285, CLOSED_COMMIT),
                blame(163, CLOSED_COMMIT, "ccccccccccc1"),
            ),
        ),
        replaced=(CLOSED_1574,),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "example_repo#1643"
    assert len(result.set_aside) == 1
    assert result.set_aside[0].place.endswith("SKILL.md:285")


def test_an_overlap_nobody_can_attribute_is_never_set_aside() -> None:
    """A hunk whose blame lists no commits counts like today; nothing is set aside."""
    result = measured(
        open_prs=(retention(blame(285), blame(163)),),
        replaced=(CLOSED_1574,),
    )

    assert result.state == "adopt"
    assert result.set_aside == ()
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


# ---- a replaces entry that is not closed, unmerged and of this repository is refused


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"state": "OPEN"}, EnumReplacesRefusal.STILL_OPEN),
        ({"state": "MERGED"}, EnumReplacesRefusal.MERGED),
        ({"state": "CLOSED", "merged": True}, EnumReplacesRefusal.MERGED),
        (
            {"repo": "OmniNode-ai/omnibase_core", "state": None},
            EnumReplacesRefusal.OTHER_REPOSITORY,
        ),
        ({"state": None}, EnumReplacesRefusal.UNREADABLE),
        ({"commits": ()}, EnumReplacesRefusal.NO_COMMITS),
    ],
    ids=[
        "still_open",
        "merged_state",
        "merged_flag",
        "other_repository",
        "unreadable",
        "no_commits",
    ],
)
def test_a_replaces_entry_that_is_not_closed_unmerged_here_is_refused(
    updates: dict[str, object], reason: EnumReplacesRefusal
) -> None:
    """Each bad entry is refused with its typed reason and no evidence is gathered."""
    result = measured(replaced=(CLOSED_1574.model_copy(update=updates),))

    assert result.state == "replaces_refused"
    assert len(result.replaces_refusals) == 1
    assert result.replaces_refusals[0].reason == reason
    assert result.evidence == ()


# ---- without a replaces entry, blame on the request changes nothing


def without_attribution(pr: ModelOpenPr) -> ModelOpenPr:
    """The same open pull request with every blame record dropped from its files."""
    assert pr.touch is not None
    return pr.model_copy(
        update={
            "touch": ModelPrTouch(
                files=tuple(
                    f.model_copy(update={"attributed": ()}) for f in pr.touch.files
                )
            )
        }
    )


def test_attributed_spans_change_nothing_without_a_replaces_entry() -> None:
    """With no replaces, a request carrying blame answers today's JSON exactly."""
    with_blame = measured()
    without = measured(open_prs=(without_attribution(RETENTION),))

    assert with_blame.state == "adopt"
    assert with_blame.model_dump(mode="json") == without.model_dump(mode="json")


# ---- the packaged contract binds the handler and its models (golden and error chains)


def contract_chain() -> tuple[
    HandlerPrDuplicateGuard, type[ModelPrDuplicateGuardRequest]
]:
    """The handler and request model exactly as the packaged contract binds them."""
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    request_type = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    return handler(), request_type


def measured_dict(state: str = "CLOSED") -> dict[str, object]:
    """The measured case as the caller posts it: one plain dict, nothing imported."""
    return {
        "repo": REPO,
        "head_ref": "omn-19998-glm-lane-route",
        "base": "main",
        "title": "feat(OMN-19998): GLM lane route and call limits",
        "now": NOW.isoformat(),
        "touch": {
            "files": [
                {"path": SKILL, "spans": [[285, 285]], "digest": ["aaa111aaa111"]},
                {"path": FAILOVER, "spans": [[163, 163]], "digest": ["bbb222bbb222"]},
            ]
        },
        "open_prs": [
            {
                "number": 1643,
                "head_ref": "omn-19998-retention-stdin",
                "base": "main",
                "title": "fix(remote-lane): stream retention over stdin (OMN-19998)",
                "draft": False,
                "touch": {
                    "files": [
                        {
                            "path": SKILL,
                            "spans": [[285, 285]],
                            "digest": ["aaa111aaa111"],
                            "attributed": [
                                {"start": 285, "end": 285, "commits": [CLOSED_COMMIT]}
                            ],
                        },
                        {
                            "path": FAILOVER,
                            "spans": [[163, 163]],
                            "digest": ["bbb222bbb222"],
                            "attributed": [
                                {"start": 163, "end": 163, "commits": [CLOSED_COMMIT]}
                            ],
                        },
                        {"path": OTHER, "spans": [[700, 700]]},
                    ]
                },
            }
        ],
        "replaced": [
            {
                "repo": REPO,
                "number": 1574,
                "state": state,
                "merged": False,
                "commits": [CLOSED_COMMIT],
            }
        ],
    }


def test_golden_chain_replaced_closed_change_is_set_aside_through_the_contract_models() -> (
    None
):
    """Through the contract's bindings, the closed change's overlaps are set aside and round trip."""
    handler, request_type = contract_chain()
    result = handler.handle(request_type.model_validate(measured_dict()))

    assert result.state == "clear"
    assert len(result.set_aside) == 2
    round_tripped = ModelPrDuplicateGuardResult.model_validate_json(
        result.model_dump_json()
    )
    assert round_tripped == result
    assert [item.place for item in round_tripped.set_aside] == [
        item.place for item in result.set_aside
    ]
    assert round_tripped.set_aside[0].commits == (CLOSED_COMMIT,)


def test_error_chain_open_replaces_entry_is_refused_with_a_typed_reason_through_the_contract_models() -> (
    None
):
    """Through the contract's bindings, an open replaced entry is refused with still_open."""
    handler, request_type = contract_chain()
    result = handler.handle(request_type.model_validate(measured_dict(state="OPEN")))
    dumped = result.model_dump_json()

    assert result.state == "replaces_refused"
    assert "still_open" in dumped
    round_tripped = ModelPrDuplicateGuardResult.model_validate_json(dumped)
    assert round_tripped == result
    assert round_tripped.replaces_refusals[0].reason is EnumReplacesRefusal.STILL_OPEN
