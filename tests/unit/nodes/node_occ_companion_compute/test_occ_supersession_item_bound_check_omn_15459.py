# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15459 AC(d): a supersession must attest the item it supersedes.

Live defect this pins (``onex_change_control#5534``, companion for
``omnibase_infra#2552``, MERGED 2026-07-30T01:53:03Z, merge commit
``34c8dacc4cab7bf0e48a616fdb9820aefb09434d``): the merged-path supersession loop
in ``compute_companion_plan`` passed the ONE shared ``downstream_check`` into
``_supersede_file`` for EVERY prior entry, so all **8** emitted
``command.supersede.2552.yaml`` files carried a byte-identical
``replacement.check_value`` --

    gh api repos/OmniNode-ai/omnibase_infra/contents/scripts/create_kafka_topics.py?ref=...
      --jq '.content' | base64 -d | grep -c 'def _build_specs'

-- standing in as the authoritative proof for eight distinct bars. Two of them
are not remotely the same bar: ``dod-occ-evidence-admissibility-validator``
declares ``uv run pytest tests/test_evidence_admissibility.py -q`` and
``dod-deploy-assessment`` declares a deploy-scope diff assertion. Neither was
run. ``OCC#5528`` (the companion one product PR earlier, for infra#2550) carries
6 of the same shape: two consecutive machine-minted companions, same producer.

This is the *wrong-item rebind* variant of the OMN-15247 laundering family, and
it is the one that scales -- supersession is the REPAIR primitive, so a producer
that can rebind any item to any passing check makes the corrective mechanism a
laundering channel, ~20 cohorts/day.

These tests drive the REAL producer entrypoint (``compute_companion_plan`` -- the
same function ``node_occ_companion_effect`` calls) against a merged-receipt
fixture built by the REAL contract renderer. No surrogate, no monkeypatching
(``feedback_test_the_artifact_that_runs``).

RED-before, verified against ``dev`` @ ``fb99acf2``:
  * ``test_merged_path_supersessions_carry_distinct_checks`` -- dev emits 4
    byte-identical ``replacement.check_value`` values across 4 distinct items.
  * ``test_each_supersession_attests_its_own_declared_check`` -- dev binds the
    admissibility item to the generic PR-files probe, not to its declared
    ``uv run pytest tests/test_evidence_admissibility.py -q``.
  * ``test_supersession_check_references_its_own_item`` -- the S2 family-binding
    shape; dev's shared probe references no anchor of the items it rebinds.
  * ``test_cohort_collision_is_refused_loudly`` /
    ``test_item_without_a_declared_check_is_refused`` -- behavioural
    "DID NOT RAISE" against dev, not a collection-time ImportError, because both
    drive ``compute_companion_plan`` and assert a broad ``ValueError``
    (``feedback_prove_red_against_exists_but_wrong``).
"""

from __future__ import annotations

import re

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    compute_contract_sha256,
    render_compute_companion_contract,
)

_DEPLOY_ID = DEPLOY_ASSESSMENT_EVIDENCE_ID

# The live instance, verbatim: OCC#5534 / omnibase_infra#2552 / OMN-15395.
_TICKET = "OMN-15395"
_REPO = "OmniNode-ai/omnibase_infra"
_PRODUCT_PR = 2552
_PRODUCT_HEAD = "f420d5fa26f704c75c3854ba2efc788d36175d13"
_OCC_PR = 5534
_OCC_HEAD = "34c8dacc4cab7bf0e48a616fdb9820aefb09434d"
_OCC_REPO = "OmniNode-ai/onex_change_control"

_FIRST_PR = 2543
_FIRST_OCC_PR = 5488
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_FIRST_SELF_BIND = f"occ-self-bind-pr-{_FIRST_OCC_PR}"

_PRODUCT_PROBE = ModelObservedProbe(
    command=(
        f"gh api repos/{_REPO}/pulls/{_PRODUCT_PR}/files --jq '[.[].filename]|length'"
    ),
    stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
    exit_code=0,
)
_OCC_PROBE = ModelObservedProbe(
    command=f"gh api repos/{_OCC_REPO}/pulls/{_OCC_PR}",
    stdout=f'{{"number":{_OCC_PR},"state":"OPEN"}}',
    exit_code=0,
)


def _merged_contract() -> str:
    """The already-merged 1st-consumer contract, from the REAL renderer.

    Rendered with the deploy + self-bind items so ``existing_entry_ids`` is a
    multi-item set whose declared bars genuinely differ -- the shape OCC#5534
    faced, and the only shape in which a shared check is observable.
    """
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
        self_bind_evidence_id=_FIRST_SELF_BIND,
        occ_pr_number=_FIRST_OCC_PR,
        occ_repo=_OCC_REPO,
        emit_deploy_assessment=True,
    )


def _declared_entry_ids(contract_text: str) -> tuple[str, ...]:
    parsed = yaml.safe_load(contract_text)
    return tuple(item["id"] for item in parsed["dod_evidence"])


def _declared_checks(contract_text: str) -> dict[str, list[str]]:
    """{item id -> every declared check_value}, whitespace-normalised."""
    parsed = yaml.safe_load(contract_text)
    return {
        str(item["id"]): [
            " ".join(str(check["check_value"]).split())
            for check in item.get("checks", [])
            if isinstance(check, dict) and check.get("check_value")
        ]
        for item in parsed["dod_evidence"]
    }


def _merged_state(contract_text: str) -> ModelOccContractState:
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=_declared_entry_ids(contract_text),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )


def _request(
    *,
    contract_states: tuple[ModelOccContractState, ...],
    occ_pr_number: int | None,
) -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"fix({_TICKET}): close the second createTopics path",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-07-30T01:53:03Z",
        product_probe=_PRODUCT_PROBE,
        occ_contract_states=contract_states,
        occ_pr_number=occ_pr_number,
        occ_head_sha=_OCC_HEAD if occ_pr_number is not None else None,
        occ_probe=_OCC_PROBE if occ_pr_number is not None else None,
    )


def _merged_plan(*, occ_pr_number: int | None = _OCC_PR) -> ModelOccCompanionPlan:
    return compute_companion_plan(
        _request(
            contract_states=(_merged_state(_merged_contract()),),
            occ_pr_number=occ_pr_number,
        )
    )


def _replacement_checks(plan: ModelOccCompanionPlan) -> dict[str, str]:
    """{superseded evidence id -> replacement.check_value} from the emitted YAML."""
    out: dict[str, str] = {}
    for companion in plan.companion_files:
        if companion.kind is not EnumCompanionFileKind.SUPERSEDE_RECEIPT:
            continue
        record = ModelReceiptSupersession.model_validate(
            yaml.safe_load(companion.content)
        )
        assert record.replacement is not None
        out[record.evidence_item_id] = " ".join(record.replacement.check_value.split())
    return out


# ---------------------------------------------------------------------------
# AC(d) core — distinctness, and per-item binding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("occ_pr_number", "label"),
    [(None, "pass 1 (no OCC PR yet)"), (_OCC_PR, "pass 2")],
)
def test_merged_path_supersessions_carry_distinct_checks(
    occ_pr_number: int | None, label: str
) -> None:
    """No two supersessions in one cohort may share a byte-identical check."""
    checks = _replacement_checks(_merged_plan(occ_pr_number=occ_pr_number))

    # Precondition: the fixture is genuinely multi-item, so the assertion below
    # cannot pass vacuously on a one-entry cohort.
    assert len(checks) >= 4, (
        f"{label}: fixture must supersede several items to exercise distinctness, "
        f"got {sorted(checks)}"
    )

    by_value: dict[str, list[str]] = {}
    for entry, value in checks.items():
        by_value.setdefault(value, []).append(entry)
    collisions = {v: sorted(e) for v, e in by_value.items() if len(e) > 1}
    assert not collisions, (
        f"{label}: byte-identical replacement.check_value across distinct "
        f"dod_evidence items — the OMN-15459 wrong-item rebind (OCC#5534): "
        f"{collisions}"
    )


def test_each_supersession_attests_its_own_declared_check() -> None:
    """Every replacement carries the check ITS OWN contract entry declares."""
    contract = _merged_contract()
    declared = _declared_checks(contract)
    checks = _replacement_checks(_merged_plan())

    for entry, value in checks.items():
        assert declared.get(entry), f"fixture defect: {entry} declares no check"
        assert value in declared[entry], (
            f"{entry}'s supersession attests {value!r}, which is NOT one of the "
            f"checks its own contract entry declares ({declared[entry]}). A "
            "replacement must prove the superseded item's bar, not another "
            "item's (OMN-15459)."
        )


def test_admissibility_item_is_not_rebound_to_the_generic_pr_probe() -> None:
    """The named live symptom: the admissibility item's own suite, not a grep.

    OMN-15459 calls this out by name — ``dod-occ-evidence-admissibility-validator``
    ended up attesting that a Kafka-topic script defines a function, while the
    admissibility suite it names went unrun.
    """
    checks = _replacement_checks(_merged_plan())
    assert ADMISSIBILITY_VALIDATOR_EVIDENCE_ID in checks

    value = checks[ADMISSIBILITY_VALIDATOR_EVIDENCE_ID]
    assert value == ADMISSIBILITY_VALIDATOR_CHECK_VALUE, (
        "the admissibility item must be superseded by ITS OWN declared check "
        f"({ADMISSIBILITY_VALIDATOR_CHECK_VALUE!r}), got {value!r}"
    )
    assert _PRODUCT_PROBE.command not in value, (
        "the admissibility item was rebound to this PR's generic files probe — "
        "the exact OCC#5534 shape"
    )


def test_supersession_check_references_its_own_item() -> None:
    """S2 family binding, mirrored: the check names something the item declares.

    The enforcing gate
    (``onex_change_control/scripts/validation/check_receipt_hardening.py``,
    OMN-15459) derives anchors from the superseded item's own declared checks —
    path tokens, quoted symbols, and PR numbers — plus the item id. This asserts
    the producer's output satisfies that rule at the source, so the companion is
    not born red against a required check.
    """
    contract = _merged_contract()
    declared = _declared_checks(contract)
    checks = _replacement_checks(_merged_plan())

    path_token = re.compile(r"[\w.\-]+/[\w./\-]+")
    for entry, value in checks.items():
        anchors: set[str] = {entry.lower()}
        anchors.update(re.findall(r"\d{2,}", entry))
        for declared_value in declared[entry]:
            anchors.update(t.lower() for t in path_token.findall(declared_value))
            anchors.update(re.findall(r"\d{2,}", declared_value))
        lowered = value.lower()
        assert any(anchor in lowered for anchor in anchors), (
            f"{entry}'s replacement check {value!r} references no anchor of the "
            f"item it supersedes (anchors: {sorted(anchors)}) — S2 family-binding "
            "violation (OMN-15459)"
        )


# ---------------------------------------------------------------------------
# The MECHANISM — a rule is not a mechanism.
# ---------------------------------------------------------------------------


def test_cohort_collision_is_refused_loudly() -> None:
    """A merged contract whose items DO share a check is refused, not emitted.

    Broad ``ValueError`` with the required PT011 ``match``, on purpose: this
    keeps the dev-side RED behavioural ("DID NOT RAISE") rather than a
    collection-time ImportError on a symbol dev does not have.
    """
    contract = _merged_contract()
    declared = _declared_checks(contract)
    # Force the degenerate corpus the guard exists for: give the deploy item the
    # admissibility item's exact declared check, so two distinct items now
    # declare one bar.
    collided = contract.replace(
        declared[_DEPLOY_ID][0], ADMISSIBILITY_VALIDATOR_CHECK_VALUE, 1
    )
    assert collided != contract, "fixture rewrite did not apply"

    with pytest.raises(ValueError, match=r"OMN-15459|wrong-item rebind") as excinfo:
        compute_companion_plan(
            _request(
                contract_states=(_merged_state(collided),),
                occ_pr_number=None,
            )
        )
    message = str(excinfo.value)
    assert _DEPLOY_ID in message
    assert ADMISSIBILITY_VALIDATOR_EVIDENCE_ID in message


def test_collision_refusal_is_a_typed_named_error() -> None:
    """The refusal is a typed error, not a bare ValueError or a silent drop."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        SupersessionCheckBindingError,
    )

    contract = _merged_contract()
    declared = _declared_checks(contract)
    collided = contract.replace(
        declared[_DEPLOY_ID][0], ADMISSIBILITY_VALIDATOR_CHECK_VALUE, 1
    )
    with pytest.raises(SupersessionCheckBindingError):
        compute_companion_plan(
            _request(
                contract_states=(_merged_state(collided),),
                occ_pr_number=None,
            )
        )


def test_item_without_a_declared_check_is_refused() -> None:
    """No derivable item-bound check → refuse, never substitute a stand-in.

    AC(d): "A producer that can only synthesize one probe per PR should supersede
    ONE item, not blanket the directory." Silently falling back to the shared
    probe is precisely the defect.
    """
    contract = _merged_contract()
    declared = _declared_checks(contract)
    # Blank the admissibility item's only check_value, leaving the item declared.
    stripped = contract.replace(declared[ADMISSIBILITY_VALIDATOR_EVIDENCE_ID][0], "", 1)
    assert stripped != contract, "fixture rewrite did not apply"

    with pytest.raises(ValueError, match=r"OMN-15459|check_value") as excinfo:
        compute_companion_plan(
            _request(
                contract_states=(_merged_state(stripped),),
                occ_pr_number=None,
            )
        )
    assert ADMISSIBILITY_VALIDATOR_EVIDENCE_ID in str(excinfo.value)


def test_invariant_falsifier_distinct_allowed_shared_refused() -> None:
    """Direct falsifier on the invariant helper, both polarities."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        SupersessionCheckBindingError,
        assert_supersession_checks_are_item_bound,
    )

    assert_supersession_checks_are_item_bound(
        {"dod-a": "check a", "dod-b": "check b"},
        ticket_id=_TICKET,
        pr_number=_PRODUCT_PR,
    )
    with pytest.raises(SupersessionCheckBindingError, match="dod-b"):
        assert_supersession_checks_are_item_bound(
            {"dod-a": "one probe", "dod-b": "one probe"},
            ticket_id=_TICKET,
            pr_number=_PRODUCT_PR,
        )


def test_declared_check_extractor_prefers_the_command_check() -> None:
    """The extractor reads the item's own entry, normalised, or returns None."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        declared_check_value_for,
    )

    parsed = yaml.safe_load(_merged_contract())
    assert (
        declared_check_value_for(parsed, ADMISSIBILITY_VALIDATOR_EVIDENCE_ID)
        == ADMISSIBILITY_VALIDATOR_CHECK_VALUE
    )
    # Multi-check item resolves deterministically to its first command check.
    first = declared_check_value_for(parsed, _FIRST_ENTRY)
    assert first == _declared_checks(_merged_contract())[_FIRST_ENTRY][0]
    # Absent item and non-mapping input are None, never a stand-in.
    assert declared_check_value_for(parsed, "dod-does-not-exist") is None
    assert declared_check_value_for(None, _FIRST_ENTRY) is None
    # Whitespace is collapsed, so the value is always single-line and safe
    # inside the receipt template's ``check_value: |-`` block scalar.
    folded = {
        "dod_evidence": [
            {
                "id": "dod-folded",
                "checks": [{"check_type": "command", "check_value": "a\n  b\tc"}],
            }
        ]
    }
    assert declared_check_value_for(folded, "dod-folded") == "a b c"


# ---------------------------------------------------------------------------
# No regression of the neighbouring paths.
# ---------------------------------------------------------------------------


def test_merged_path_still_supersedes_every_frozen_entry() -> None:
    """OMN-14623/OMN-15485 invariants hold: one net-new supersede per entry."""
    contract = _merged_contract()
    plan = _merged_plan()
    paths = {f.path for f in plan.companion_files}

    for entry in _declared_entry_ids(contract):
        supersede = (
            f"drift/dod_receipts/{_TICKET}/{entry}/command.supersede.{_PRODUCT_PR}.yaml"
        )
        assert supersede in paths, f"{entry} lost its supersession rebind"
        assert f"drift/dod_receipts/{_TICKET}/{entry}/command.yaml" not in paths


def test_supersessions_still_rebind_to_this_product_pr() -> None:
    """Changing the attested BAR must not un-bind the rebind to this PR."""
    plan = _merged_plan()
    for companion in plan.companion_files:
        if companion.kind is not EnumCompanionFileKind.SUPERSEDE_RECEIPT:
            continue
        record = ModelReceiptSupersession.model_validate(
            yaml.safe_load(companion.content)
        )
        replacement = record.replacement
        assert replacement is not None
        assert replacement.pr_number == _PRODUCT_PR
        assert replacement.commit_sha == _PRODUCT_HEAD
        assert replacement.probe_command == _PRODUCT_PROBE.command
        assert companion.contract_entry_sha256.startswith("sha256:")


def test_fresh_path_downstream_receipt_is_unchanged() -> None:
    """The fresh path's own receipt still carries the per-PR downstream probe."""
    plan = compute_companion_plan(_request(contract_states=(), occ_pr_number=_OCC_PR))
    downstream = next(
        f
        for f in plan.companion_files
        if f.kind is EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        and f.path.endswith(
            f"dod-{_REPO.replace('/', '-')}-pr-{_PRODUCT_PR}/command.yaml"
        )
    )
    parsed = yaml.safe_load(downstream.content)
    assert str(_PRODUCT_PR) in parsed["check_value"]
