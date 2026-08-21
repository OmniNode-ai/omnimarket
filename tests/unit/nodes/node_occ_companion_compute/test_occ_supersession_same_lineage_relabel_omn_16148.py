# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16148: a same-lineage relabel must not trip the OMN-15459 S1 guard.

Live defect this pins (``onex_change_control#6633``, hand-authored companion
for ``omnimarket#2089``, dry-run CI observed on
``omnimarket/actions/runs/32107711708/job/95620427646``): the merged-path
supersession loop in ``compute_companion_plan`` blanket-re-supersedes EVERY
declared entry in a ticket's merged contract to the current product PR, one
``command.supersede.<pr_number>.yaml`` per entry, each attesting that entry's
OWN declared ``check_value`` (the OMN-15459 fix). ``OMN-15800``'s real
contract already carries a legitimate same-bar relabel: an entry whose
``evidence_item_id`` mislabeled an honest content-presence probe as a
"deploy-live-probe" is corrected by a NET-NEW entry
(``evidence_artifact: "supersedes_dod_evidence:<the mislabeled id>"``) that
deliberately keeps the IDENTICAL ``check_value`` -- only the label was wrong,
not the substance. ``assert_supersession_checks_are_item_bound`` treats ANY
two distinct items sharing a byte-identical check as the OMN-15459 wrong-item
rebind (OCC#5534 shape: one probe standing in for N unrelated bars) and
refuses the whole cohort -- which is correct for OCC#5534 but a false
positive for a declared, same-lineage relabel, where the shared value is not
a laundered stand-in but the SAME bar, deliberately re-declared.

This is a fixed reproduction of the OMN-15800/omnimarket#2089 shape, not the
live ticket itself (which is already patched around by the OCC#6633
hand-authored companion) -- ``test_reconstructed_omn_15800_cohort_no_longer_raises``
mirrors the exact two colliding ids from the live error message.

RED-before, verified against ``dev`` @ ``88f62b95``:
  * ``test_declared_lineage_relabel_is_exempt_from_the_collision_guard`` --
    dev raises ``SupersessionCheckBindingError`` for a same-lineage relabel
    pair that shares a check_value on purpose.
  * ``test_unrelated_items_sharing_a_check_are_still_refused`` -- pins that
    the OCC#5534 shape (no declared lineage between the colliding items)
    still raises after the fix; this guard's core purpose is unchanged.
  * ``test_reconstructed_omn_15800_cohort_no_longer_raises`` -- the exact
    live collision, by id, no longer raises.

Sibling of ``test_occ_supersession_item_bound_check_omn_15459.py``, whose
fixtures and helpers this file reuses.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    SupersessionCheckBindingError,
    assert_supersession_checks_are_item_bound,
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    compute_contract_sha256,
    render_compute_companion_contract,
)

_TICKET = "OMN-15801"
_REPO = "OmniNode-ai/omnimarket"
_FIRST_PR = 2032
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"

_PRODUCT_PR = 2089
_PRODUCT_HEAD = "abc123def456abc123def456abc123def456abc"

_PRODUCT_PROBE = ModelObservedProbe(
    command=f"gh api repos/{_REPO}/pulls/{_PRODUCT_PR}/files --jq '[.[].filename]|length'",
    stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
    exit_code=0,
)


def _entry_dict(
    *, entry_id: str, check_value: str, evidence_artifact: str | None = None
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": entry_id,
        "description": "Fixture entry.",
        "source": "manual",
    }
    if evidence_artifact is not None:
        entry["evidence_artifact"] = evidence_artifact
    entry["checks"] = [{"check_type": "command", "check_value": check_value}]
    return entry


def _append_entries(contract_text: str, *entries: dict[str, object]) -> str:
    """Append well-formed dod_evidence entries to already-rendered YAML text.

    Round-trips through ``yaml.safe_load``/``yaml.safe_dump`` rather than
    hand-writing YAML scalars — several fixture ``check_value`` strings embed
    literal double quotes (real ``gh api`` one-liners), which is exactly the
    shape that breaks naive string-templated YAML.
    """
    parsed = yaml.safe_load(contract_text)
    parsed["dod_evidence"].extend(entries)
    return yaml.safe_dump(parsed, sort_keys=False, default_flow_style=False)


def _declared_entry_ids(contract_text: str) -> tuple[str, ...]:
    parsed = yaml.safe_load(contract_text)
    return tuple(item["id"] for item in parsed["dod_evidence"])


def _merged_state(contract_text: str) -> ModelOccContractState:
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=_declared_entry_ids(contract_text),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )


def _request(contract_text: str) -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"fix({_TICKET}): unrelated follow-up",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-08-10T05:17:14Z",
        product_probe=_PRODUCT_PROBE,
        occ_contract_states=(_merged_state(contract_text),),
        occ_pr_number=None,
        occ_head_sha=None,
        occ_probe=None,
    )


def _relabeled_contract() -> str:
    """OMN-15800's real shape, trimmed: entry A, then B relabels A verbatim.

    Built on the real renderer's base entry (so entry hashing/admissibility
    plumbing is genuine), then hand-appends the relabel pair the renderer has
    no parameter for.
    """
    base = render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
    )
    declared = yaml.safe_load(base)["dod_evidence"][0]["checks"][0]["check_value"]
    relabel_id = f"{_FIRST_ENTRY}-relabel"
    return _append_entries(
        base,
        _entry_dict(
            entry_id=relabel_id,
            check_value=declared,
            evidence_artifact=f"supersedes_dod_evidence:{_FIRST_ENTRY}",
        ),
    )


def test_declared_lineage_relabel_is_exempt_from_the_collision_guard() -> None:
    """A relabel entry sharing its target's exact check must not be refused."""
    contract = _relabeled_contract()
    plan = compute_companion_plan(_request(contract))

    # No exception means the plan computed cleanly. Both entries in the
    # relabel pair must still have been superseded to this product PR — the
    # exemption changes only the collision VERDICT, not which entries get
    # rebound (OMN-14623/OMN-15485 invariant, unchanged).
    paths = {f.path for f in plan.companion_files}
    for entry in (_FIRST_ENTRY, f"{_FIRST_ENTRY}-relabel"):
        assert (
            f"drift/dod_receipts/{_TICKET}/{entry}/command.supersede.{_PRODUCT_PR}.yaml"
            in paths
        ), f"{entry} lost its supersession rebind under the exemption"


def test_unrelated_items_sharing_a_check_are_still_refused() -> None:
    """No declared lineage between the colliding items -> still a hard refusal.

    Same two-entry shape as the relabel fixture, but WITHOUT the
    ``evidence_artifact`` marker linking them — this is exactly the OCC#5534
    shape (one probe standing in for a DIFFERENT, unrelated item) that
    OMN-15459 exists to catch. The exemption must not weaken this case.
    """
    base = render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
    )
    declared = yaml.safe_load(base)["dod_evidence"][0]["checks"][0]["check_value"]
    unrelated_id = "dod-unrelated-item-no-lineage"
    text = _append_entries(
        base, _entry_dict(entry_id=unrelated_id, check_value=declared)
    )

    with pytest.raises(SupersessionCheckBindingError) as excinfo:
        compute_companion_plan(_request(text))
    message = str(excinfo.value)
    assert _FIRST_ENTRY in message
    assert unrelated_id in message


def test_reconstructed_omn_15800_cohort_no_longer_raises() -> None:
    """The exact live collision (by id) from the omnimarket#2089 dry-run.

    ``onex_change_control#6633``'s error named these two ids verbatim,
    rebinding to an identical check_value. ``dod-source-presence-probe-...``
    is the declared relabel of ``dod-deploy-live-probe-...`` in the real
    OMN-15800 contract (evidence_artifact: supersedes_dod_evidence:...).
    """
    deploy_id = "dod-deploy-live-probe-pr-2032-rebind-f2958714"
    presence_id = "dod-source-presence-probe-pr-2032-rebind-f2958714"
    shared_check = (
        'body="$(gh api "repos/OmniNode-ai/omnimarket/contents/'
        'src/omnimarket/projection/runner.py?ref=f2958714f54a3372b09e6f96d0e4597610c2ecb7" '
        "--jq '.content' | base64 -d)\" && count=\"$(printf '%s' \"$body\" | grep -c "
        '\'publish_snapshot_delta\')" && [ "$count" -ge 1 ]'
    )
    text = yaml.safe_dump(
        {
            "ticket_id": _TICKET,
            "dod_evidence": [
                _entry_dict(entry_id=deploy_id, check_value=shared_check),
                _entry_dict(
                    entry_id=presence_id,
                    check_value=shared_check,
                    evidence_artifact=f"supersedes_dod_evidence:{deploy_id}",
                ),
            ],
        },
        sort_keys=False,
        default_flow_style=False,
    )

    plan = compute_companion_plan(_request(text))
    paths = {f.path for f in plan.companion_files}
    for entry in (deploy_id, presence_id):
        assert (
            f"drift/dod_receipts/{_TICKET}/{entry}/command.supersede.{_PRODUCT_PR}.yaml"
            in paths
        )


def test_invariant_falsifier_lineage_exempts_only_declared_pairs() -> None:
    """Direct falsifier on the mechanism, both polarities, with lineage."""
    parsed_contract = {
        "dod_evidence": [
            {
                "id": "dod-a",
                "checks": [{"check_type": "command", "check_value": "probe"}],
            },
            {
                "id": "dod-b",
                "evidence_artifact": "supersedes_dod_evidence:dod-a",
                "checks": [{"check_type": "command", "check_value": "probe"}],
            },
        ]
    }
    # Declared lineage (dod-b supersedes dod-a) exempts the identical check.
    assert_supersession_checks_are_item_bound(
        {"dod-a": "probe", "dod-b": "probe"},
        ticket_id=_TICKET,
        pr_number=_PRODUCT_PR,
        parsed_contract=parsed_contract,
    )
    # No parsed_contract -> no lineage info -> prior (strict) behaviour holds.
    with pytest.raises(SupersessionCheckBindingError):
        assert_supersession_checks_are_item_bound(
            {"dod-a": "probe", "dod-b": "probe"},
            ticket_id=_TICKET,
            pr_number=_PRODUCT_PR,
        )
    # Two items sharing a check with NEITHER declaring lineage to the other
    # (dod-c is unrelated) is still refused even with parsed_contract given.
    parsed_contract_unrelated = {
        "dod_evidence": [
            {
                "id": "dod-a",
                "checks": [{"check_type": "command", "check_value": "probe"}],
            },
            {
                "id": "dod-c",
                "checks": [{"check_type": "command", "check_value": "probe"}],
            },
        ]
    }
    with pytest.raises(SupersessionCheckBindingError):
        assert_supersession_checks_are_item_bound(
            {"dod-a": "probe", "dod-c": "probe"},
            ticket_id=_TICKET,
            pr_number=_PRODUCT_PR,
            parsed_contract=parsed_contract_unrelated,
        )
