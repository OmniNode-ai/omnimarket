# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16859: a minted receipt's key must agree with the check_type it backs.

THE DEFECT, measured four times on 2026-08-28 (OMN-16838 / OMN-16842 /
OMN-16844 / OMN-16901, each a separate lane, each a hand-authored receipt on a
blocked critical path). ``node_occ_companion_compute`` declares the OMN-16434
diff-derived behavior item as ``check_type: "test_passes"`` and then writes its
receipt at ``<item>/command.yaml``. ``validator_occ_merge_eligibility``
(omnibase_core) resolves an item's receipt at
``drift/dod_receipts/<ticket>/<item>/<check_type>.yaml`` and keys it
``<ticket>:<item>:<check_type>``, so it looks for ``:test_passes``, finds only
the ``:command`` sibling, and returns
``{"eligible": false, "reason": "missing_receipt"}``. The companion goes
BLOCKED and — because OCC companions merge FIRST — the product PR goes with it.

THE SECOND HALF, live on OCC#7465 (minted 2026-08-28T21:25:23Z, i.e. AFTER
OMN-16892 fixed the born-path MINT):
``drift/dod_receipts/OMN-16442/dod-occ-diff-derived-behavior-proof/command.supersede.2192.yaml``
supersedes an item whose contract check is ``check_type: "test_passes"``. That
one is worse than a wrong filename. ``resolve_supersession`` globs
``<check_type>.supersede.*.yaml``, so a record filed under ``command.`` for a
``test_passes`` item is never even a candidate: the rebind to the 2nd consumer
PR silently does not apply, and nothing reports it.

WHY EMIT-MATCHING AND NOT DECLARE-``command``, decided at the consumer:
``contract_compliance_check._check_test_passes`` (onex_change_control) is, since
OMN-16824, an EXECUTED alias of ``command`` — it runs ``check_value`` and reads
its exit status — and ``node_dod_verify`` has always executed it. So declaring
``test_passes`` is an honest statement about a check the OCC runner really does
execute; nothing about the declaration is a claim this producer makes falsely.
``check_contract_substance_floor.derive_proof_tier`` additionally keys
``check_type == "test_passes"`` to tier L1. Declaring ``command`` to match the
filename would therefore discard the author's stated intent at both consumers to
paper over a path bug, and would orphan the four hand-authored
``test_passes.yaml`` receipts already in the corpus (AC4 keeps them). The
receipt follows the declaration; the declaration does not follow the receipt.

Proof structure (feedback_prove_red_against_exists_but_wrong): every leg drives
the REAL ``compute_companion_plan`` and, where it can, the REAL omnibase_core
consumer (``resolve_supersession``) rather than restating the producer's own
convention back to itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)
from omnibase_core.validation.validator_receipt_supersession import (
    resolve_supersession,
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
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    compute_contract_sha256,
    render_compute_companion_contract,
)

_TICKET = "OMN-16859"
_REPO = "OmniNode-ai/omnimarket"
_PR = 2193
_HEAD = "c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6d7d8d9d0"
_SRC_FILE = "src/omnimarket/nodes/node_occ_companion_compute/handlers/x.py"
_TEST_FILE = "tests/unit/nodes/node_occ_companion_compute/test_x_omn16859.py"

_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_PR = 7500
_OCC_HEAD = "e1e2e3e4e5e6e7e8e9e0f1f2f3f4f5f6f7f8f9f0"

# The FIRST consumer, whose companion is already merged — the shape the
# supersede path re-binds. Its contract carries a ``test_passes`` behavior item
# because its own diff touched a test file: exactly OCC#7465 / OMN-16442.
_FIRST_PR = 2192
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_FIRST_OCC_PR = 7465


def _probe(pr: int) -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {pr} --repo {_REPO} --json number,state,headRefName",
        stdout=f'{{"number":{pr},"state":"OPEN"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": _HEAD,
        "pr_title": f"fix({_TICKET}): receipt key follows the declared check_type",
        "pr_body": f"Closes {_TICKET}",
        "run_timestamp": "2026-08-29T00:00:00Z",
        "product_probe": _probe(_PR),
        "changed_files": (_SRC_FILE, _TEST_FILE),
        "diff_total_lines": 60,
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _contract_text(plan: ModelOccCompanionPlan) -> str:
    return next(
        f.content
        for f in plan.companion_files
        if f.kind == EnumCompanionFileKind.CONTRACT
    )


def _declared_check_types(contract_text: str) -> dict[str, list[str]]:
    """``{item id -> [declared check_type, ...]}`` from the emitted contract."""
    parsed = yaml.safe_load(contract_text)
    assert isinstance(parsed, dict)
    return {
        str(item["id"]): [
            str(check.get("check_type") or "")
            for check in item.get("checks", [])
            if isinstance(check, dict)
        ]
        for item in parsed["dod_evidence"]
        if isinstance(item, dict)
    }


def _receipt_paths(plan: ModelOccCompanionPlan) -> dict[str, str]:
    """``{evidence item id -> receipt basename}`` over plain receipt emissions."""
    out: dict[str, str] = {}
    for companion in plan.companion_files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        parts = companion.path.split("/")
        out[parts[-2]] = parts[-1]
    return out


# ---------------------------------------------------------------------------
# AC1 — the MINT path on the compute producer.
# ---------------------------------------------------------------------------


def test_behavior_item_receipt_is_filed_under_its_declared_check_type() -> None:
    """AC1. The four-times-hand-patched defect, stated as one assertion.

    RED before the fix: the receipt is minted at ``command.yaml`` while the
    contract entry declares ``test_passes``, which is precisely the
    ``missing_receipt`` occ-preflight failure on OMN-16838 / OMN-16842 /
    OMN-16844 / OMN-16901.
    """
    plan = compute_companion_plan(_request())
    contract = _contract_text(plan)
    declared = _declared_check_types(contract)

    # Precondition: this diff really does yield the behavior item (if it did
    # not, the assertion below would pass vacuously).
    assert declared[BEHAVIOR_PROOF_EVIDENCE_ID] == ["test_passes"]

    paths = _receipt_paths(plan)
    assert paths[BEHAVIOR_PROOF_EVIDENCE_ID] == "test_passes.yaml"


def test_behavior_receipt_records_the_check_type_it_is_filed_under() -> None:
    """AC1, second half. A right filename with a ``command`` body still fails.

    ``resolve_supersession`` key-validates a record's own ``check_type`` field
    against the key it is filed under, and a receipt whose recorded type
    disagrees with its item is self-contradictory evidence even where nothing
    currently rejects it. Path and body move together or not at all.
    """
    plan = compute_companion_plan(_request())
    receipt = next(
        yaml.safe_load(f.content)
        for f in plan.companion_files
        if f.path.endswith(f"/{BEHAVIOR_PROOF_EVIDENCE_ID}/test_passes.yaml")
    )
    assert receipt["check_type"] == "test_passes"
    assert receipt["evidence_item_id"] == BEHAVIOR_PROOF_EVIDENCE_ID


def test_no_pytest_target_keeps_the_command_shape_byte_for_byte() -> None:
    """CONTROL. The OWED branch is untouched — no receipt moves that did not have to.

    Without this the fix could pass leg 1 by renaming every receipt, which would
    invalidate the entire merged corpus rather than the one item at issue.
    """
    plan = compute_companion_plan(_request(changed_files=(_SRC_FILE,)))
    declared = _declared_check_types(_contract_text(plan))
    assert declared[ADMISSIBILITY_VALIDATOR_EVIDENCE_ID] == ["command"]

    paths = _receipt_paths(plan)
    assert paths[ADMISSIBILITY_VALIDATOR_EVIDENCE_ID] == "command.yaml"
    assert BEHAVIOR_PROOF_EVIDENCE_ID not in paths


# ---------------------------------------------------------------------------
# AC2 — the fail-closed invariant, over every emission site.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param((_SRC_FILE, _TEST_FILE), id="behavior-item"),
        pytest.param((_SRC_FILE,), id="owed-branch"),
        pytest.param(("deploy/compose.yaml", _TEST_FILE), id="deploy-assessment"),
    ],
)
def test_every_emitted_receipt_agrees_with_its_declared_check_type(
    changed: tuple[str, ...],
) -> None:
    """AC2. Agreement holds for EVERY emitted receipt, not just the behavior one.

    Path basename, the receipt's own recorded ``check_type``, and the contract
    entry's declared ``check_type`` are one value at three sites; this asserts
    all three agree across each branch the producer can take. A future emission
    site that hardcodes ``command.yaml`` fails here rather than in CI on someone
    else's blocked PR.
    """
    plan = compute_companion_plan(
        _request(
            changed_files=changed,
            occ_pr_number=_OCC_PR,
            occ_head_sha=_OCC_HEAD,
            occ_repo=_OCC_REPO,
            occ_probe=_probe(_OCC_PR),
        )
    )
    declared = _declared_check_types(_contract_text(plan))

    for companion in plan.companion_files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        parts = companion.path.split("/")
        item_id, basename = parts[-2], parts[-1]
        body = yaml.safe_load(companion.content)
        recorded = str(body["check_type"])
        assert basename == f"{recorded}.yaml", (
            f"{companion.path} is filed under {basename!r} but records "
            f"check_type {recorded!r}"
        )
        if item_id in declared:
            assert recorded in declared[item_id], (
                f"{companion.path} records check_type {recorded!r}, which the "
                f"contract entry {item_id!r} does not declare "
                f"({declared[item_id]})"
            )


def test_every_declared_item_has_a_receipt_at_its_declared_key() -> None:
    """AC2, completeness. The occ-preflight question, asked at mint time.

    Eligibility iterates the contract's ``dod_evidence`` and demands a receipt
    at each item's declared key. Asking that of the producer's own plan turns a
    generator/consumer mismatch into a producer-side failure instead of a
    ``missing_receipt`` an operator re-diagnoses from scratch. Honestly
    superseded items are excused exactly as the validator excuses them.
    """
    plan = compute_companion_plan(
        _request(
            occ_pr_number=_OCC_PR,
            occ_head_sha=_OCC_HEAD,
            occ_repo=_OCC_REPO,
            occ_probe=_probe(_OCC_PR),
        )
    )
    contract = yaml.safe_load(_contract_text(plan))
    superseded = {
        str(marker).split(":", 1)[1]
        for item in contract["dod_evidence"]
        for marker in [item.get("evidence_artifact") or ""]
        if str(marker).startswith("supersedes_dod_evidence:")
    }
    emitted = {
        f.path.split("/")[-2]: f.path.split("/")[-1]
        for f in plan.companion_files
        if f.kind is not EnumCompanionFileKind.CONTRACT
    }
    for item in contract["dod_evidence"]:
        item_id = str(item["id"])
        if item_id in superseded:
            continue
        types = [str(c.get("check_type") or "") for c in item.get("checks", [])]
        assert emitted.get(item_id) in {f"{t}.yaml" for t in types}, (
            f"declared item {item_id!r} (check_type {types}) has no emitted "
            f"receipt at its declared key; occ-preflight reports this as "
            f"missing_receipt"
        )


# ---------------------------------------------------------------------------
# The SUPERSEDE path — the OCC#7465 shape.
# ---------------------------------------------------------------------------


def _merged_first_consumer_contract() -> str:
    """The already-merged 1st-consumer contract, from the REAL renderer.

    Its diff touched a test file, so it declares the behavior item as
    ``test_passes`` — the OCC#7465 / OMN-16442 shape the supersede renderer got
    wrong.
    """
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
        self_bind_evidence_id=f"occ-self-bind-pr-{_FIRST_OCC_PR}",
        occ_pr_number=_FIRST_OCC_PR,
        occ_repo=_OCC_REPO,
        changed_files=(_SRC_FILE, _TEST_FILE),
    )


def _merged_plan() -> tuple[ModelOccCompanionPlan, str]:
    contract_text = _merged_first_consumer_contract()
    parsed = yaml.safe_load(contract_text)
    state = ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=tuple(str(i["id"]) for i in parsed["dod_evidence"]),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )
    plan = compute_companion_plan(
        _request(
            occ_contract_states=(state,),
            occ_pr_number=_OCC_PR,
            occ_head_sha=_OCC_HEAD,
            occ_repo=_OCC_REPO,
            occ_probe=_probe(_OCC_PR),
        )
    )
    return plan, contract_text


def _supersede_files(plan: ModelOccCompanionPlan) -> dict[str, tuple[str, str]]:
    """``{superseded item id -> (basename, file content)}``."""
    return {
        f.path.split("/")[-2]: (f.path.split("/")[-1], f.content)
        for f in plan.companion_files
        if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
    }


def test_supersede_of_a_test_passes_item_is_filed_under_test_passes() -> None:
    """The OCC#7465 defect. RED: ``command.supersede.2193.yaml`` for a test_passes item.

    Three fields move together — the filename ``resolve_supersession`` globs,
    the record's own key field it validates, and the ``supersedes`` pointer at
    the base receipt.
    """
    plan, _ = _merged_plan()
    basename, content = _supersede_files(plan)[BEHAVIOR_PROOF_EVIDENCE_ID]
    assert basename == f"test_passes.supersede.{_PR}.yaml"

    record = ModelReceiptSupersession.model_validate(yaml.safe_load(content))
    assert record.check_type == "test_passes"
    assert record.supersedes.endswith(f"/{BEHAVIOR_PROOF_EVIDENCE_ID}/test_passes.yaml")
    assert record.replacement is not None
    assert record.replacement.check_type == "test_passes"


def test_command_items_keep_their_command_supersede_filename() -> None:
    """CONTROL. Every ``command`` item's supersede shape is unchanged.

    The merged corpus is full of ``command.supersede.<NNNN>.yaml`` records; a
    fix that renamed them all would invalidate the chains it was meant to
    repair.
    """
    plan, _ = _merged_plan()
    files = _supersede_files(plan)
    assert files[_FIRST_ENTRY][0] == f"command.supersede.{_PR}.yaml"
    record = ModelReceiptSupersession.model_validate(
        yaml.safe_load(files[_FIRST_ENTRY][1])
    )
    assert record.check_type == "command"


def test_core_resolve_supersession_finds_the_rebind(tmp_path: Path) -> None:
    """THE CONSUMER, executed. Not a restatement of the producer's convention.

    ``resolve_supersession`` globs ``<check_type>.supersede.*.yaml`` and rejects
    a record whose declared key does not match the key it is filed under. This
    writes the producer's real output to disk and asks omnibase_core the exact
    question occ-preflight asks. RED today twice over: the glob misses
    ``command.supersede.<NNNN>.yaml`` entirely for a ``test_passes`` item, so
    the 2nd consumer's rebind never applies and nothing says so.
    """
    plan, _ = _merged_plan()
    receipts_dir = tmp_path / "drift" / "dod_receipts"
    for companion in plan.companion_files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        target = tmp_path / companion.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(companion.content, encoding="utf-8")

    resolution = resolve_supersession(
        receipts_dir,
        _TICKET,
        BEHAVIOR_PROOF_EVIDENCE_ID,
        "test_passes",
        current_pr_number=_PR,
    )
    assert resolution is not None, (
        "core found no supersession record for the test_passes key — the "
        "producer filed the rebind under a key nothing resolves"
    )
    assert resolution.error is None, resolution.error
    assert resolution.receipt is not None
    assert resolution.receipt.pr_number == _PR
