"""One rule for who owns a shared red check, in the node both readers call (OMN-19998).

2026-10-18: ``Hostile Review Gate`` was red on 5 omniclaude PRs while a live lane held a CLAIM with
``cause=omniclaude:Hostile Reviewer (adversarial gate)``, the root the gate needs. The merge pulse and the throughput
tick disagreed about which check name owns an inherited red. ``cause_family`` is the shared rule; the readers' own
tests are ``tests/test_queue_status_shared_cause.py`` (the pulse) and ``test_throughput_tick.py`` (the tick).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
handler = importlib.import_module(
    "omnimarket.nodes.node_cause_claim_ownership_effect.handler"
)

ROOT_CHECK, GATE, SIBLING, OTHER = (
    "Hostile Reviewer (adversarial gate)",
    "Hostile Review Gate",
    "Coverage Gate",
    "Lint",
)
INHERITED = [[GATE, 5, [ROOT_CHECK]], [SIBLING, 2, [ROOT_CHECK]]]


def test_a_claim_on_the_root_owns_every_gate_inheriting_it():
    assert handler.cause_family(ROOT_CHECK, INHERITED) == sorted(
        [ROOT_CHECK, GATE, SIBLING]
    )


def test_a_claim_on_an_inheriting_gate_is_a_claim_on_its_root_and_the_roots_other_gates():
    assert handler.cause_family(GATE, INHERITED) == sorted([ROOT_CHECK, GATE, SIBLING])


def test_a_check_outside_the_needs_graph_is_its_own_family():
    assert handler.cause_family(OTHER, INHERITED) == [OTHER]
    assert handler.cause_family(GATE, None) == [GATE]
    assert handler.cause_family(GATE, []) == [GATE]


def test_two_roots_keep_their_families_apart():
    inherited = [[GATE, 5, [ROOT_CHECK]], ["Docs Gate", 1, ["Docs build"]]]
    assert handler.cause_family("Docs build", inherited) == ["Docs Gate", "Docs build"]
    assert handler.cause_family(ROOT_CHECK, inherited) == sorted([ROOT_CHECK, GATE])


def test_claim_names_cause_reads_the_cause_cell_or_the_check_and_repo_on_the_first_line():
    row = "2026-10-18T15:00:00Z | CLAIM | lane=x | ticket=OMN-1 | cause=omniclaude:Hostile Review Gate | est ~1"
    assert handler.claim_names_cause(row, "omniclaude", GATE)
    assert not handler.claim_names_cause(row, "omnimarket", GATE)  # another repository
    assert not handler.claim_names_cause(
        row, "omniclaude", ROOT_CHECK
    )  # another check: the family is the caller's rule
    assert handler.claim_names_cause(
        "2026-10-18T15:00:00Z | CLAIM | lane=y | scope=fix Lint on omniclaude",
        "omniclaude",
        OTHER,
    )
    assert not handler.claim_names_cause(
        "2026-10-18T15:00:00Z | CLAIM | lane=z | pr=omniclaude#1 | est ~1",
        "omniclaude",
        OTHER,
    )


# ---- class cause-claim-check-name-unmatched (OMN-17427): one spelling of a check name on both sides ----------------
# 2026-10-19T21:43Z: cause=omnibase_infra:CI_Summary and cause=RSD:Hostile_Review_Gate read UNOWNED (replay:
# tests/test_cause_claim_check_name_replay.py).


def test_canonical_check_folds_case_spaces_underscores_and_hyphens():
    assert {
        handler.canonical_check(x)
        for x in ("CI Summary", "CI_Summary", "ci-summary", " CI  _Summary ")
    } == {"ci summary"}
    assert (
        handler.canonical_check("repo-evidence / dod-verify")
        == "repo evidence / dod verify"
    )


@pytest.mark.parametrize(
    "cell",
    [
        "omnibase_infra:CI_Summary",
        "omnibase_infra:ci-summary",
        "OmniNode-ai/omnibase_infra:CI Summary",
        "Omnibase_Infra:CI Summary",
    ],
)
def test_claim_names_cause_matches_every_spelling_of_the_check(cell):
    row = f"2026-10-19T21:30:44Z | CLAIM | lane=x | ticket=OMN-17427 | cause={cell} | est ~2"
    assert handler.claim_names_cause(row, "omnibase_infra", "CI Summary")
    assert not handler.claim_names_cause(row, "omnibase_infra", "Lane Census Staleness")
    assert not handler.claim_names_cause(row, "omnimarket", "CI Summary")


def test_the_lane_name_alone_does_not_name_the_check_in_another_spelling():
    row = "2026-10-19T21:30:44Z | CLAIM | lane=cause-omnibase_infra-ci-summary-2125 | cause=omnibase_infra:Lint"
    assert not handler.claim_names_cause(row, "omnibase_infra", "CI Summary")
    assert handler.claim_names_cause(
        "t | CLAIM | lane=y | scope=fix CI_Summary on omnibase_infra",
        "omnibase_infra",
        "CI Summary",
    )


def test_detector_and_near_spelling():
    row = "t | CLAIM | lane=x | cause=RSD:Hostile_Review_Gate | cause=RSD:Hostile Review Gat | cause=RSD:Lint"
    assert handler.spelling_only_match(row, "RSD", "Hostile Review Gate") == [
        "Hostile_Review_Gate"
    ]
    assert (
        handler.spelling_only_match(
            "t | CLAIM | cause=RSD:hostile review gate", "RSD", "Hostile Review Gate"
        )
        == []
    )
    assert handler.near_miss_cells(row, "RSD", "Hostile Review Gate") == [
        "Hostile Review Gat"
    ]
    assert handler.near_miss_cells(
        "t | CLAIM | cause=RSD:Hostile Review", "RSD", "Hostile Review Gate"
    ) == ["Hostile Review"]
    assert handler.near_miss_cells(row, "omnimarket", "Hostile Review Gate") == []
    assert (
        handler.near_miss_cells(
            f"t | CLAIM | cause=omniclaude:{ROOT_CHECK}", "omniclaude", GATE
        )
        == []
    )
