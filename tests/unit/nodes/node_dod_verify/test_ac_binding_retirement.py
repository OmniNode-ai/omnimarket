# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20157: audited withdrawals never accept a proposal or erase coverage gaps."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dod_verify.__main__ import _build_probe_stdout
from omnimarket.nodes.node_dod_verify.models.model_ac_binding_retirement import (
    ModelAcBindingRetirement,
    ModelAcBindingRetirementResolution,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.ac_binding_retirements import (
    resolve_retirements,
)
from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
    derive_falsifier_items,
    self_accepted_bindings,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.test_dod_verify_acceptance_basis import _contract, _run
from tests.unit.nodes.node_dod_verify.omn_19428_occ_tree import occ_contract

pytestmark = pytest.mark.unit


def _entry(**changes: Any) -> dict[str, Any]:
    return {
        "item": "draft",
        "label": "AC1",
        "reason_kind": "no_longer_applicable",
        "reason": "This proposal is obsolete.",
        "retired_by": "review-lane",
        "retired_at": "2026-10-08T12:00:00Z",
        **changes,
    }


def _item(item_id: str, *labels: str, accepted_by: str | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "description": "binding retirement evidence",
        "binds_ac": list(labels),
        "ac_bindings": [
            {
                "label": label,
                "proposed_by": item_id,
                "criterion_hash": "a" * 64,
                **({"accepted_by": accepted_by} if accepted_by is not None else {}),
            }
            for label in labels
        ],
        "checks": [{"check_type": "command", "check_value": "true"}],
    }


def _carrier(*entries: Any) -> dict[str, Any]:
    return {**_item("carrier"), "supersedes_ac_binding": list(entries)}


def _derive(contract: dict[str, Any]) -> Any:
    return derive_falsifier_items(
        contract,
        contract["dod_evidence"],
        repo_candidates=("omnimarket",),
        path_exists=lambda _repo, _path: True,
        declared_runner=lambda _repo, _path: "uv run pytest",
    )[1]


@pytest.mark.parametrize("with_retirement", [False, True])
@pytest.mark.parametrize("self_accepted", [False, True])
def test_retired_draft_no_longer_blocks_real_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_retirement: bool,
    self_accepted: bool,
) -> None:
    extra = [_item("draft", "AC1", accepted_by="draft" if self_accepted else None)]
    if with_retirement:
        extra.append(_carrier(_entry()))
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"}, extra_items=extra
    )
    summary = _derive(contract)
    state = _run(tmp_path, monkeypatch, contract)
    draft_check = next(check for check in state.checks if check.evidence_id == "draft")
    assert draft_check.status is EnumEvidenceCheckStatus.VERIFIED
    if with_retirement:
        assert summary.self_accepted_bindings == ()
        assert state.status is EnumDodVerifyStatus.VERIFIED
        assert state.acceptance_retired_bindings == summary.retired_bindings
        assert (
            "draft:AC1 retired_by=review-lane" in state.acceptance_retired_bindings[0]
        )
        assert draft_check.binds_ac == draft_check.draft_binds_ac == ()
        assert json.loads(_build_probe_stdout(state))[
            "acceptance_retired_bindings"
        ] == list(state.acceptance_retired_bindings)
    else:
        assert state.status is EnumDodVerifyStatus.SKIPPED
        assert state.error_message is not None
        assert state.error_message.startswith("AC_BINDING_SELF_ACCEPTED")
        assert summary.self_accepted_bindings == (
            f"draft:AC1 accepted_by={'draft' if self_accepted else '<none>'}",
        )
        assert state.acceptance_retired_bindings == ()


@pytest.mark.parametrize("reaccepted", [False, True])
def test_accepted_binding_cannot_be_retired(reaccepted: bool) -> None:
    target = _item("draft", "AC1", accepted_by=None if reaccepted else "reviewer")
    items = [target, _carrier(_entry())]
    if reaccepted:
        independent = deepcopy(target)
        independent["id"] = "independent"
        independent["ac_bindings"][0]["accepted_by"] = "reviewer"
        items.append(independent)
    before = deepcopy(items)
    resolution = resolve_retirements(items)
    assert resolution.applied == ()
    assert "RETIREMENT_BINDING_ACCEPTED" in resolution.refused[0]
    assert self_accepted_bindings(items, retired=resolution.pairs) == ()
    assert target["binds_ac"] == ["AC1"]
    assert items == before


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({}, "reason_kind"),
        ({}, "reason"),
        ({"reason": "   "}, None),
        ({"reason": " too short "}, None),
        ({"reason_kind": "superseded_by"}, None),
        ({"superseded_by": "other"}, None),
        ({"superseded_by": None}, None),
        ({}, "retired_by"),
        ({"retired_by": " "}, None),
        ({}, "retired_at"),
        ({"retired_at": "2026-10-08T12:00:00+00:00"}, None),
        ({"retired_at": "2026-10-08T12:00:00.000Z"}, None),
        ({"retired_at": "2026-02-30T12:00:00Z"}, None),
        ({"retired_at": "2026-10-08T25:00:00Z"}, None),
        ({"reason_kind": "made_up"}, None),
        ({"item": 42}, None),
        ({"label": " "}, None),
        ({"carried_by": "forged"}, None),
        ({"unknown": "extra"}, None),
    ],
)
def test_invalid_entries_are_refused_and_draft_still_blocks(
    changes: dict[str, Any], missing: str | None
) -> None:
    entry = _entry(**changes)
    if missing:
        del entry[missing]
    items = [_item("draft", "AC1"), _carrier(entry)]
    resolution = resolve_retirements(items)
    assert resolution.applied == ()
    assert len(resolution.refused) == 1
    code = "RETIREMENT_UNTYPED" if missing == "reason_kind" else "RETIREMENT_INVALID"
    assert code in resolution.refused[0]
    assert self_accepted_bindings(items, retired=resolution.pairs) == (
        "draft:AC1 accepted_by=<none>",
    )


def test_legacy_entry_is_explicitly_untyped() -> None:
    items = [
        _item("draft", "AC1"),
        _carrier({"item": "draft", "label": "AC1", "reason": "legacy explanation"}),
    ]
    resolution = resolve_retirements(items)
    assert resolution.pairs == frozenset()
    assert resolution.refused[0].startswith("carrier:draft:AC1 RETIREMENT_UNTYPED:")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"item": "missing"}, "RETIREMENT_TARGET_UNKNOWN"),
        ({"label": "AC99"}, "RETIREMENT_LABEL_NOT_BOUND"),
        (
            {"reason_kind": "superseded_by", "superseded_by": "missing"},
            "RETIREMENT_SUPERSEDER_INVALID",
        ),
        (
            {"reason_kind": "superseded_by", "superseded_by": "draft"},
            "RETIREMENT_SUPERSEDER_INVALID",
        ),
        (
            {"reason_kind": "superseded_by", "superseded_by": "other"},
            "RETIREMENT_SUPERSEDER_INVALID",
        ),
    ],
)
def test_invalid_targets_and_superseders(changes: dict[str, Any], code: str) -> None:
    items = [_item("draft", "AC1"), _item("other", "AC2"), _carrier(_entry(**changes))]
    resolution = resolve_retirements(items)
    assert resolution.applied == ()
    assert code in resolution.refused[0]


def test_target_cannot_carry_its_own_retirement() -> None:
    item = _item("draft", "AC1")
    item["supersedes_ac_binding"] = [_entry()]
    resolution = resolve_retirements([item])
    assert resolution.applied == ()
    assert "RETIREMENT_SELF_CARRIED" in resolution.refused[0]


@pytest.mark.parametrize("cycle", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_retired_superseder_refuses_both_entries(cycle: bool, reverse: bool) -> None:
    entries = [
        _entry(reason_kind="superseded_by", superseded_by="second"),
        _entry(
            item="second",
            **(
                {"reason_kind": "superseded_by", "superseded_by": "draft"}
                if cycle
                else {}
            ),
        ),
    ]
    if reverse:
        entries.reverse()
    items = [_item("draft", "AC1"), _item("second", "ac-1"), _carrier(*entries)]
    resolution = resolve_retirements(items)
    assert resolution.applied == ()
    assert len(resolution.refused) == 2
    assert all(
        "RETIREMENT_SUPERSEDER_INVALID" in refusal for refusal in resolution.refused
    )
    assert len(self_accepted_bindings(items, retired=resolution.pairs)) == 2


@pytest.mark.parametrize("label", ["AC1", " ac-1 ", "ac_1"])
def test_supersession_and_duplicate_first_wins(label: str) -> None:
    first = _entry(label=label, reason_kind="superseded_by", superseded_by="second")
    items = [
        _item("draft", "AC1"),
        _item("second", "ac_1"),
        _carrier(first),
        {**_carrier(_entry()), "id": "later"},
    ]
    before = deepcopy(items)
    resolution = resolve_retirements(items)
    assert len(resolution.applied) == 1
    assert resolution.applied[0].carried_by == "carrier"
    assert resolution.refused == ()
    assert resolution.pairs == frozenset({("draft", "AC1")})
    assert "reason=superseded_by:second" in resolution.applied_summaries[0]
    assert self_accepted_bindings(items, retired=resolution.pairs) == (
        "second:ac_1 accepted_by=<none>",
    )
    assert items == before


@pytest.mark.parametrize("remaining", [(), ("AC2",)])
def test_collector_removes_only_retired_claims_and_still_runs_checks(
    tmp_path: Path, remaining: tuple[str, ...]
) -> None:
    target = _item("draft", "ac-1", *remaining)
    path = occ_contract(tmp_path, [target, _carrier(_entry(label=" AC_1 "))])
    results = EvidenceCollector().collect("OMN-9999", contract_path=path)
    target_result = next(result for result in results if result.evidence_id == "draft")
    assert target_result.status is EnumEvidenceCheckStatus.VERIFIED
    assert target_result.binds_ac == remaining
    assert target_result.draft_binds_ac == remaining


def test_binds_ac_without_binding_records_is_retirable() -> None:
    target = _item("draft", "AC1")
    del target["ac_bindings"]
    resolution = resolve_retirements([target, _carrier(_entry())])
    assert resolution.pairs == frozenset({("draft", "AC1")})
    assert resolution.refused == ()


def test_only_retired_binding_leaves_criterion_unbound_in_real_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC1 supplies runnable accepted evidence; only the withdrawn draft claims AC2.
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"},
        extra_items=[_item("draft", "AC2"), _carrier(_entry(label="AC2"))],
    )
    contract["requirements"][0]["acceptance"].append(
        {"id": "AC2", "statement": "other criterion"}
    )
    summary = _derive(contract)
    assert summary.self_accepted_bindings == ()
    assert summary.unbound_criteria == ("AC2",)
    state = _run(tmp_path, monkeypatch, contract)
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.acceptance_unbound_criteria == ("AC2",)
    assert state.error_message is not None
    assert state.error_message.startswith("NO_ACCEPTANCE_CHECKS")


def test_remaining_draft_error_includes_retirement_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"},
        extra_items=[
            _item("draft", "AC1"),
            _item("remaining", "AC1"),
            _carrier(_entry()),
        ],
    )
    state = _run(tmp_path, monkeypatch, contract)
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.error_message is not None
    assert " Retired bindings (not counted): draft:AC1" in state.error_message
    assert state.acceptance_self_accepted_bindings == (
        "remaining:AC1 accepted_by=<none>",
    )


def test_refusal_is_visible_but_does_not_demote_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"},
        extra_items=[_carrier(_entry(item="missing"))],
    )
    state = _run(tmp_path, monkeypatch, contract)
    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.acceptance_retired_bindings == ()
    assert "RETIREMENT_TARGET_UNKNOWN" in state.acceptance_refused_retirements[0]
    assert json.loads(_build_probe_stdout(state))[
        "acceptance_refused_retirements"
    ] == list(state.acceptance_refused_retirements)


def test_accepted_binding_refusal_preserves_collector_claim(tmp_path: Path) -> None:
    path = occ_contract(
        tmp_path, [_item("draft", "AC1", accepted_by="reviewer"), _carrier(_entry())]
    )
    results = EvidenceCollector().collect("OMN-9999", contract_path=path)
    target = next(result for result in results if result.evidence_id == "draft")
    assert target.binds_ac == ("AC1",)
    assert target.draft_binds_ac == ()


def test_retirement_does_not_suppress_failing_item_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _item("draft", "AC1")
    target["checks"][0]["check_value"] = "retired-item-check"
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"},
        extra_items=[target, _carrier(_entry())],
    )
    state = _run(
        tmp_path, monkeypatch, contract, failing=frozenset({"retired-item-check"})
    )
    assert state.status is EnumDodVerifyStatus.FAILED
    assert state.acceptance_retired_bindings
    target_result = next(
        check for check in state.checks if check.evidence_id == "draft"
    )
    assert target_result.status is EnumEvidenceCheckStatus.FAILED
    assert target_result.binds_ac == target_result.draft_binds_ac == ()


def test_malformed_entries_are_reported_in_contract_order() -> None:
    resolution = resolve_retirements(
        [
            None,
            _carrier(None, "not an entry", _entry(item="missing")),
            {**_carrier(_entry(item="also-missing")), "id": "later"},
        ]
    )
    assert resolution.applied == ()
    assert len(resolution.refused) == 4
    assert "RETIREMENT_INVALID" in resolution.refused[0]
    assert "RETIREMENT_INVALID" in resolution.refused[1]
    assert resolution.refused[2].startswith(
        "carrier:missing:AC1 RETIREMENT_TARGET_UNKNOWN"
    )
    assert resolution.refused[3].startswith(
        "later:also-missing:AC1 RETIREMENT_TARGET_UNKNOWN"
    )


@pytest.mark.parametrize("target_derived", [False, True])
def test_derived_falsifier_cannot_be_retirement_target_or_superseder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_derived: bool
) -> None:
    entry = (
        _entry(item="ac-falsifier-ac1")
        if target_derived
        else _entry(reason_kind="superseded_by", superseded_by="ac-falsifier-ac1")
    )
    contract = _contract(
        falsifiers={"AC1": "uv run pytest tests/test_a.py -q"},
        extra_items=[_item("draft", "AC1"), _carrier(entry)],
    )
    state = _run(tmp_path, monkeypatch, contract)
    assert state.acceptance_retired_bindings == ()
    code = (
        "RETIREMENT_TARGET_UNKNOWN"
        if target_derived
        else "RETIREMENT_SUPERSEDER_INVALID"
    )
    assert code in state.acceptance_refused_retirements[0]
    claims = {check.evidence_id: check.binds_ac for check in state.checks}
    assert claims["ac-falsifier-ac1"] == ("AC1",)
    assert claims["draft"] == ("AC1",)


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    entry = ModelAcBindingRetirement.model_validate(
        {**_entry(), "carried_by": "carrier"}
    )
    resolution = ModelAcBindingRetirementResolution(applied=(entry,))
    assert resolution.applied_summaries == (
        "draft:AC1 retired_by=review-lane at=2026-10-08T12:00:00Z reason=no_longer_applicable (This proposal is obsolete.)",
    )
    for model in (entry, resolution):
        with pytest.raises(ValidationError):
            model.model_validate({**model.model_dump(), "unknown": "extra"})
        with pytest.raises(ValidationError):
            model.__setattr__(next(iter(type(model).model_fields)), "changed")
