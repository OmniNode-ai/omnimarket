# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13888: the autobind producer never mints a receipt it cannot bind per entry.

THE DEFECT, measured on ``onex_change_control`` dev at ``51b5a53f86``:
``contracts/OMN-17530.yaml`` declared 6 ``dod_evidence`` items against 9
receipt directories. The three extras
(``dod-OmniNode-ai-omnibase_infra-pr-3326`` / ``-pr-3328`` / ``-pr-3332``)
carried a whole-file ``contract_sha256`` and NO ``contract_entry_sha256``.

Cause, in this module: ``compute_companion_plan``'s merged path
(``state.exists and state.merged``) appended ONLY the OCC self-bind entry to
the frozen merged contract, on the assumption that this PR's own
``evidence_id`` was already among ``state.existing_entry_ids``. That holds for
the FIRST consumer of a ticket — whose companion authored the contract — and
is false for every 2nd-and-later product PR citing the same ticket. Those
consumers still minted a net-new per-PR downstream receipt, ``_entry_hash_for``
returned ``None`` for it, and ``render_compute_receipt`` silently dropped the
field via ``_COMPUTE_RECEIPT_HEAD_TEMPLATE_NO_ENTRY``.

The resulting ORPHAN receipt pins the whole contract file, so every later
append to that contract BY ANY LANE restales it — which append-locked
``contracts/OMN-17530.yaml`` for the fleet — and the S2 supersede path cannot
repair it, because S2 derives a replacement's anchors from the superseded
item's OWN declared checks and an undeclared item declares none.

RED before, verified against ``omnimarket`` dev @ ``8cd15c84``: every
``test_red_*``-marked assertion below fails there. The fresh path (a contract
authored by the SAME companion) never had this defect and is asserted here as
the unchanged control.
"""

from __future__ import annotations

import pytest
import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    OrphanReceiptBindingError,
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    append_dod_evidence_items,
    compute_contract_sha256,
    render_compute_companion_contract,
    render_compute_downstream_dod_evidence_item,
)

_TICKET = "OMN-17530"
_REPO = "OmniNode-ai/omnibase_infra"
_FIRST_PR = 3319
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_SECOND_PR = 3326
_SECOND_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_SECOND_PR}"
_ADMISSIBILITY_ENTRY = "dod-occ-evidence-admissibility-validator"
_HEAD = "6dd2dedef6232ce762fb1473b3d8ba5a97a11e59"

_PROBE = ModelObservedProbe(
    command=f"gh pr view {_SECOND_PR} --repo {_REPO} --json number,state,headRefName",
    stdout=f'{{"number":{_SECOND_PR},"state":"OPEN"}}',
    exit_code=0,
)


def _first_consumer_contract() -> str:
    """The contract the FIRST consumer's companion authored and merged."""
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
    )


def _merged_state(contract_text: str) -> ModelOccContractState:
    parsed = yaml.safe_load(contract_text)
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=tuple(item["id"] for item in parsed["dod_evidence"]),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )


def _request(
    contract_text: str | None, *, occ_pr_number: int | None = None
) -> ModelOccCompanionRequest:
    states = () if contract_text is None else (_merged_state(contract_text),)
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_SECOND_PR,
        pr_head_sha=_HEAD,
        pr_title=f"fix({_TICKET}): the second consumer of this ticket",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-09-08T15:38:54Z",
        product_probe=_PROBE,
        occ_contract_states=states,
        occ_pr_number=occ_pr_number,
        occ_head_sha=_HEAD if occ_pr_number is not None else None,
        occ_probe=_PROBE if occ_pr_number is not None else None,
    )


def _files_by_path(plan: object) -> dict[str, object]:
    return {f.path: f for f in plan.companion_files}  # type: ignore[attr-defined]


def _contract_and_receipt(
    plan: object,
) -> tuple[str, dict[str, object]]:
    files = _files_by_path(plan)
    contract = files[f"contracts/{_TICKET}.yaml"]
    receipt = files[f"drift/dod_receipts/{_TICKET}/{_SECOND_ENTRY}/command.yaml"]
    return contract.content, yaml.safe_load(receipt.content)  # type: ignore[attr-defined]


def test_red_merged_path_declares_this_prs_own_entry() -> None:
    """The missing append. On dev the committed contract never declares it."""
    plan = compute_companion_plan(_request(_first_consumer_contract()))
    contract_text, _ = _contract_and_receipt(plan)
    declared = [item["id"] for item in yaml.safe_load(contract_text)["dod_evidence"]]
    # The first consumer's own two rows, then THIS PR's row appended last.
    assert declared == [
        _FIRST_ENTRY,
        _ADMISSIBILITY_ENTRY,
        _SECOND_ENTRY,
    ]


def test_red_merged_path_downstream_receipt_carries_an_entry_hash() -> None:
    """The receipt binds its OWN entry, not the whole file."""
    plan = compute_companion_plan(_request(_first_consumer_contract()))
    contract_text, receipt = _contract_and_receipt(plan)
    entry_hash = receipt.get("contract_entry_sha256")
    assert entry_hash, "the downstream receipt was minted whole-file-bound (orphan)"
    assert entry_hash == compute_contract_entry_sha256(
        yaml.safe_load(contract_text), _SECOND_ENTRY
    ), "the receipt's entry hash is not recomputable from the contract it ships with"


def test_red_the_appended_entry_leaves_the_first_consumers_hash_unchanged() -> None:
    """Append-invariance: the first consumer's receipt is not restaled."""
    merged = _first_consumer_contract()
    before = compute_contract_entry_sha256(yaml.safe_load(merged), _FIRST_ENTRY)
    plan = compute_companion_plan(_request(merged))
    contract_text, _ = _contract_and_receipt(plan)
    after = compute_contract_entry_sha256(yaml.safe_load(contract_text), _FIRST_ENTRY)
    assert before == after


def test_merged_path_pass_two_declares_both_the_pr_entry_and_the_self_bind() -> None:
    """The self-bind append (OMN-14623) is preserved, not replaced."""
    plan = compute_companion_plan(
        _request(_first_consumer_contract(), occ_pr_number=8710)
    )
    contract_text, receipt = _contract_and_receipt(plan)
    declared = [item["id"] for item in yaml.safe_load(contract_text)["dod_evidence"]]
    assert declared == [
        _FIRST_ENTRY,
        _ADMISSIBILITY_ENTRY,
        _SECOND_ENTRY,
        "occ-self-bind-pr-8710",
    ]
    assert receipt["contract_entry_sha256"]

    self_bind = _files_by_path(plan)[
        f"drift/dod_receipts/{_TICKET}/occ-self-bind-pr-8710/command.yaml"
    ]
    assert yaml.safe_load(self_bind.content)["contract_entry_sha256"]  # type: ignore[attr-defined]


def test_no_receipt_in_the_plan_is_minted_without_an_entry_hash() -> None:
    """Fleet invariant, not one receipt: every minted receipt binds per entry."""
    for occ_pr in (None, 8710):
        plan = compute_companion_plan(
            _request(_first_consumer_contract(), occ_pr_number=occ_pr)
        )
        contract = yaml.safe_load(
            _files_by_path(plan)[f"contracts/{_TICKET}.yaml"].content  # type: ignore[attr-defined]
        )
        for path, spec in _files_by_path(plan).items():
            if not path.startswith(f"drift/dod_receipts/{_TICKET}/"):
                continue
            data = yaml.safe_load(spec.content)  # type: ignore[attr-defined]
            node = data.get("replacement") if "replacement" in data else data
            assert node.get("contract_entry_sha256"), f"{path} has no entry hash"
            assert node["contract_entry_sha256"] == compute_contract_entry_sha256(
                contract, node["evidence_item_id"]
            ), f"{path} carries an entry hash the shipped contract cannot recompute"


def test_fresh_path_is_the_unchanged_control() -> None:
    """The first consumer never had this defect; it still does not."""
    plan = compute_companion_plan(_request(None))
    contract_text, _ = _contract_and_receipt(plan)
    declared = [item["id"] for item in yaml.safe_load(contract_text)["dod_evidence"]]
    assert declared[0] == _SECOND_ENTRY


def test_mint_refuses_when_the_entry_cannot_be_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed, loudly, naming ticket + PR + item — never a silent orphan."""
    import omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute as handler

    monkeypatch.setattr(
        handler, "render_compute_downstream_dod_evidence_item", lambda **_: ""
    )
    with pytest.raises(OrphanReceiptBindingError) as excinfo:
        compute_companion_plan(_request(_first_consumer_contract()))
    message = str(excinfo.value)
    assert _TICKET in message
    assert f"{_REPO}#{_SECOND_PR}" in message
    assert _SECOND_ENTRY in message
    assert "contract_entry_sha256" in message


def test_standalone_item_renderer_matches_the_fresh_contract_byte_for_byte() -> None:
    """One authoring home: an appended row equals the row a fresh contract has."""
    item = render_compute_downstream_dod_evidence_item(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_SECOND_PR,
        evidence_id=_SECOND_ENTRY,
    )
    fresh = render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_SECOND_PR,
        evidence_id=_SECOND_ENTRY,
    )
    assert item in fresh


def test_append_refuses_a_shape_it_would_silently_drop() -> None:
    """A bare text concatenation parses fine and LOSES the row. This refuses.

    ``yaml.safe_dump`` emits sequence items at column 0; concatenating the
    renderer's 2-space-indented block onto that parses WITHOUT error and yields
    a contract missing the appended item — the worst possible input for a
    producer that then hashes against it.
    """
    normalised = yaml.safe_dump(
        yaml.safe_load(_first_consumer_contract()), sort_keys=False
    )
    item = render_compute_downstream_dod_evidence_item(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_SECOND_PR,
        evidence_id=_SECOND_ENTRY,
    )
    naive = yaml.safe_load(normalised + item)
    assert _SECOND_ENTRY not in [i["id"] for i in naive["dod_evidence"]], (
        "the fixture no longer reproduces the silent-drop shape"
    )
    repaired = yaml.safe_load(append_dod_evidence_items(normalised, [item]))
    assert _SECOND_ENTRY in [i["id"] for i in repaired["dod_evidence"]]


def test_append_is_byte_preserving_on_the_canonical_shape() -> None:
    merged = _first_consumer_contract()
    item = render_compute_downstream_dod_evidence_item(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_SECOND_PR,
        evidence_id=_SECOND_ENTRY,
    )
    result = append_dod_evidence_items(merged, [item])
    assert result.startswith(merged)
    assert result == merged + item
