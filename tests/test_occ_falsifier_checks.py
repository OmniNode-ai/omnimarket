# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20153 AC1 -- an author's accepted falsifier becomes a runnable check.

Today only 1 of 41 tickets carries a check written from its own acceptance
criteria (quality-evidence audit, 2026-09-30). The falsifiers are already in
the contract, transcribed by occ-autobind from the ticket's creation revision.
This module pins the pure derivation that turns each one into a dod_evidence
item, and the honest refusal for a falsifier a machine cannot run.

The contracts used below are copies of the shape of the real OCC contracts
minted today (OMN-20110, OMN-19790, OMN-20028), reduced to the fields the
derivation reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    derive_falsifier_items,
    parse_falsifier_command,
)

pytestmark = pytest.mark.unit

_ACCEPTED = {"accepted_by": "author-uuid", "accepted_at": "2026-09-30T01:00:00Z"}


def _contract(
    criteria: dict[str, str], *, accepted: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """A contract in the shape occ-autobind mints: requirements + ac_bindings."""
    labels = tuple(criteria) if accepted is None else accepted
    return {
        "ticket_id": "OMN-20999",
        "requirements": [
            {
                "id": "req-transcribed-acceptance-criteria",
                "statement": "transcribed",
                "acceptance": [
                    {"id": label, "statement": f"{label}: {text}"}
                    for label, text in criteria.items()
                ],
            }
        ],
        "dod_evidence": [
            {
                "id": "dod-OmniNode-ai-omnimarket-pr-3103",
                "checks": [{"check_type": "command", "check_value": "true"}],
                "binds_ac": list(labels),
                "ac_bindings": [
                    {
                        "label": label,
                        "criterion_hash": "a" * 64,
                        "proposed_by": "occ-autobind",
                        **_ACCEPTED,
                    }
                    for label in labels
                ],
            }
        ],
    }


def _derive(
    contract: dict[str, Any],
    *,
    repos: tuple[str, ...] = ("omnimarket",),
    exists: bool = True,
):
    return derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=repos,
        path_exists=lambda _repo, _path: exists,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "uv run pytest tests/hooks/test_hook_emit_journal.py -q -k append_does_not_scan",
            "uv run pytest tests/hooks/test_hook_emit_journal.py -q -k append_does_not_scan",
        ),
        (
            "`uv run pytest tests/unit/models/events/work -k legacy_claim_fixtures_parse -v` fails on a fixture",
            "uv run pytest tests/unit/models/events/work -k legacy_claim_fixtures_parse -v",
        ),
        (
            "uv run pytest tests/test_x.py -k one_row_per_label_key -v in omnimarket",
            "uv run pytest tests/test_x.py -k one_row_per_label_key -v",
        ),
        (
            "pytest tests/unit/lab_work -k placement",
            "uv run pytest tests/unit/lab_work -k placement",
        ),
        (
            "uv run pytest tests/a.py tests/b.py -q, then the readback",
            "uv run pytest tests/a.py tests/b.py -q",
        ),
    ],
)
def test_selector_is_extracted_and_normalised(text: str, expected: str) -> None:
    parsed = parse_falsifier_command(text)
    assert parsed is not None
    assert parsed.command == expected


@pytest.mark.parametrize(
    "text",
    [
        "the fold unit test",
        "uv run pytest over the verifier tests",
        "uv run pytest omnibase_infra tests for reconcile-host",
        "select count(*) from delegation_eval_items on the dev lane",
        "the lab step's two reads differ",
        "uv run pytest ../escape/test_x.py -q",
        "uv run pytest /abs/test_x.py -q",
    ],
)
def test_unrunnable_falsifier_yields_no_command(text: str) -> None:
    assert parse_falsifier_command(text) is None


def test_shell_metacharacters_never_reach_the_command() -> None:
    """The command is rebuilt from allowlisted tokens, never sliced from prose."""
    parsed = parse_falsifier_command("uv run pytest tests/x.py; rm -rf ~")
    assert parsed is not None
    assert parsed.command == "uv run pytest tests/x.py"
    piped = parse_falsifier_command("uv run pytest tests/x.py -k a_b | sh")
    assert piped is not None
    assert piped.command == "uv run pytest tests/x.py -k a_b"


def test_selector_becomes_bound_item() -> None:
    contract = _contract(
        {
            "AC1": "journal append does no scan -- falsifier: uv run pytest tests/hooks/test_hook_emit_journal.py -q -k append_does_not_scan",
            "AC2": "the drainer lists one batch -- falsifier: uv run pytest tests/hooks/test_hook_emit_journal.py -q -k list_pending_limit",
        }
    )
    items, summary = _derive(contract)
    assert [item["id"] for item in items] == ["ac-falsifier-ac1", "ac-falsifier-ac2"]
    first = items[0]
    assert first["binds_ac"] == ["AC1"]
    check = first["checks"][0]
    assert check["check_type"] == "test_passes"
    assert (
        check["check_value"]
        == "uv run pytest tests/hooks/test_hook_emit_journal.py -q -k append_does_not_scan"
    )
    assert check["cwd"] == "${OMNI_HOME}/omnimarket"
    assert summary.declared_falsifier_count == 2
    assert summary.runnable_count == 2
    assert summary.unrunnable_labels == ()


def test_selector_becomes_bound_item_prose_falsifier_mints_nothing() -> None:
    contract = _contract(
        {
            "AC1": "rows deduplicate -- falsifier: the lab-step exposure read returns a fixture row without data_source: fixture",
            "AC2": "count is stable -- falsifier: select count(*) from delegation_eval_items on the dev lane returns 0",
        }
    )
    items, summary = _derive(contract)
    assert items == []
    assert summary.declared_falsifier_count == 2
    assert summary.runnable_count == 0
    assert summary.unrunnable_labels == ("AC1", "AC2")


def test_unaccepted_criterion_is_never_derived() -> None:
    """A draft binding is a proposal; only the author's acceptance is trusted."""
    contract = _contract(
        {
            "AC1": "a -- falsifier: uv run pytest tests/test_a.py -q",
            "AC2": "b -- falsifier: uv run pytest tests/test_b.py -q",
        },
        accepted=("AC1",),
    )
    items, summary = _derive(contract)
    assert [item["binds_ac"] for item in items] == [["AC1"]]
    assert summary.declared_falsifier_count == 1


def test_no_falsifier_marker_means_no_declaration() -> None:
    contract = _contract({"AC1": "uv run pytest tests/test_a.py -q"})
    items, summary = _derive(contract)
    assert items == []
    assert summary.declared_falsifier_count == 0


def test_contract_without_requirements_declares_nothing() -> None:
    contract = {"ticket_id": "OMN-1", "dod_evidence": [{"id": "x", "checks": []}]}
    items, summary = _derive(contract)
    assert items == []
    assert summary.declared_falsifier_count == 0
    assert summary.runnable_count == 0


def test_repo_is_the_one_whose_clone_holds_the_named_path() -> None:
    contract = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
    )
    items, _ = derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=("omnibase_infra", "omniclaude"),
        path_exists=lambda repo, _path: repo == "omniclaude",
    )
    assert items[0]["checks"][0]["cwd"] == "${OMNI_HOME}/omniclaude"


def test_named_repo_hint_wins_when_it_holds_the_path() -> None:
    contract = _contract(
        {
            "AC1": "a -- falsifier: uv run pytest tests/test_a.py -q in omnibase_internal"
        },
    )
    items, _ = derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=("omnimarket",),
        path_exists=lambda _repo, _path: True,
    )
    assert items[0]["checks"][0]["cwd"] == "${OMNI_HOME}/omnibase_internal"


def test_missing_path_still_mints_so_it_fails_visibly() -> None:
    """A falsifier naming a test that does not exist must FAIL, not vanish.

    OMN-19533: a falsifier that names a nonexistent path was never run by any
    receipt. Minting it against the first candidate makes pytest exit 4, and
    the item reads FAILED.
    """
    contract = _contract({"AC1": "a -- falsifier: uv run pytest tests/test_gone.py -q"})
    items, summary = _derive(contract, exists=False)
    assert len(items) == 1
    assert summary.runnable_count == 1


def test_no_candidate_repository_is_unrunnable_not_guessed() -> None:
    contract = _contract({"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"})
    items, summary = _derive(contract, repos=())
    assert items == []
    assert summary.unrunnable_labels == ("AC1",)
