# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16071 Defect 1: the OCC companion writer must be strictly ADD-ONLY.

THE GATE ESCAPE, named precisely. ``assert_append_only_emissions`` (OMN-15485)
is the producer-side mechanism that is supposed to make an in-place rewrite of a
merged receipt unreachable from any emission site. It has one exemption::

    if _SUPERSEDE_FILENAME_RE.search(filename):
        continue

That asks *"is this filename shaped like a correction?"* — never *"is this path
already merged?"* — and filename shape is not evidence of novelty. The merged
path files every rebind at ``<check_type>.supersede.<pr_number>.yaml``, keyed to
the PRODUCT PR number, which is CONSTANT across every authoring pass for that
same product PR. So the 2nd, 3rd ... pass for one product PR renders the exact
same supersede paths, and the guard waves every one of them through as an "add"
while they are already merged bytes on ``dev``.

``ModelOccContractState`` is why the guard cannot know better: it carries
``existing_entry_ids`` — a set of DIRECTORIES — and nothing at all about which
FILES are merged. The mechanism was built without file-level knowledge, so the
supersede exemption was the only way to let a legal correction through.

LIVE REPRODUCTION, from this ticket's own record (OCC#6616, companion for
``omninode_infra#925``, 11 merged files rewritten in place): eight previously
merged ``.supersede.925.yaml`` files, minted ``created_at: 2026-08-17T07:19:22Z``
by an earlier pass for that same PR#925, were re-rendered with
``created_at: 17:14:25Z`` and re-bound **in place** by the next pass. The
ticket's own words: *"not idempotent against a third touch of the same product
PR ... this second pass rewrites those supersede files instead of creating a
fresh generation."*

WHY IT PRESENTS AS A WEDGED COMPANION RATHER THAN SILENT CORRUPTION. Since
OMN-16071's own PR #2086 the pre-push ``_assert_append_only`` refuses any
receipt whose git status is not ``A``, so the mutation is caught — by aborting
the WHOLE mint. That hardened the GUARD, not the WRITER, which is exactly what
this ticket's standing DO-NOT-FLIP verdict says. These tests pin the WRITER.

THE FIX THESE PIN, in two halves:

* ``merged_receipt_paths`` on ``ModelOccContractState`` gives the pure planner
  file-level knowledge, and the supersede exemption becomes conditional on it.
  Empty tuple reproduces today's behavior exactly, so no shipped shape moves
  until the read-EFFECT starts populating it.
* When the product-PR-keyed supersede name is already taken, the rebind is
  filed under a fresh generation keyed to the OCC companion PR doing it —
  a net-new add, which is what the ticket asks for ("express a genuine change
  as a net-new ``.supersede.<NNNN>.yaml`` file"). The suffix stays a single
  integer so it still matches the strict ``\\.supersede\\.\\d+\\.yaml$`` shape
  both this producer and ``resolve_supersession`` require.

RED-before, against ``dev`` @ ``482648e1`` (behavioural, not import errors —
``feedback_prove_red_against_exists_but_wrong``): every leg below marked RED
fails on the shipped producer because it emits the already-merged path.
"""

from __future__ import annotations

import re
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
    AppendOnlyEmissionError,
    assert_append_only_emissions,
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
    ModelOccCompanionPlan,
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

# The live instance, verbatim: OCC#6616 / omninode_infra#925 / OMN-15922.
_TICKET = "OMN-15922"
_REPO = "OmniNode-ai/omninode_infra"
_PRODUCT_PR = 925
_PRODUCT_HEAD = "9a2361e5c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6"
_OCC_REPO = "OmniNode-ai/onex_change_control"

# The pass that already merged, and the pass that collided with it.
_FIRST_PR = 916
_FIRST_OCC_PR = 6555
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_FIRST_SELF_BIND = f"occ-self-bind-pr-{_FIRST_OCC_PR}"
_SECOND_OCC_PR = 6616

_SUPERSEDE_SHAPE_RE = re.compile(r"\.supersede\.\d+\.yaml$")

_PRODUCT_PROBE = ModelObservedProbe(
    command=f"gh api repos/{_REPO}/pulls/{_PRODUCT_PR}/files --jq '[.[].filename]|length'",
    stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
    exit_code=0,
)
_OCC_PROBE = ModelObservedProbe(
    command=f"gh api repos/{_OCC_REPO}/pulls/{_SECOND_OCC_PR}",
    stdout=f'{{"number":{_SECOND_OCC_PR},"state":"OPEN"}}',
    exit_code=0,
)


def _merged_contract() -> str:
    """The already-merged 1st-consumer contract, from the REAL renderer."""
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


def _merged_state(
    contract_text: str, *, merged_receipt_paths: tuple[str, ...] = ()
) -> ModelOccContractState:
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=_declared_entry_ids(contract_text),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
        merged_receipt_paths=merged_receipt_paths,
    )


def _request(
    *,
    merged_receipt_paths: tuple[str, ...] = (),
    occ_pr_number: int | None = _SECOND_OCC_PR,
) -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"fix({_TICKET}): 2nd consumer rebinds the merged evidence",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-08-17T17:14:25Z",
        product_probe=_PRODUCT_PROBE,
        occ_contract_states=(
            _merged_state(
                _merged_contract(), merged_receipt_paths=merged_receipt_paths
            ),
        ),
        occ_pr_number=occ_pr_number,
        occ_head_sha="e1e2e3e4e5e6e7e8e9e0f1f2f3f4f5f6f7f8f9f0"
        if occ_pr_number is not None
        else None,
        occ_probe=_OCC_PROBE if occ_pr_number is not None else None,
    )


def _paths(plan: ModelOccCompanionPlan) -> set[str]:
    return {f.path for f in plan.companion_files}


def _first_pass_supersede_paths() -> tuple[str, ...]:
    """The supersede paths the FIRST pass for this product PR already merged.

    Derived from the real producer's own first-pass plan rather than restated
    as literals — the collision is between two runs of the same code, so the
    fixture has to be that code's own output or the test proves nothing.
    """
    plan = compute_companion_plan(_request())
    return tuple(
        sorted(
            f.path
            for f in plan.companion_files
            if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
        )
    )


# ---------------------------------------------------------------------------
# AC1 — the invariant itself: path membership decides, not filename shape.
# ---------------------------------------------------------------------------


def _file(path: str) -> ModelCompanionFile:
    return ModelCompanionFile(
        path=path,
        content="---\n",
        kind=EnumCompanionFileKind.SUPERSEDE_RECEIPT,
        ticket_id=_TICKET,
    )


def test_invariant_refuses_a_supersede_emission_at_an_already_merged_path() -> None:
    """RED. A supersede-shaped filename is NOT proof the path is net-new.

    This is the whole gate escape in one assertion: the emission below is
    named exactly like a legal correction and is, byte for byte, a rewrite of
    something already on ``dev``.
    """
    already_merged = _first_pass_supersede_paths()[0]
    request = _request(merged_receipt_paths=(already_merged,))

    with pytest.raises(AppendOnlyEmissionError) as excinfo:
        assert_append_only_emissions([_file(already_merged)], request)

    message = str(excinfo.value)
    assert already_merged in message
    assert "already merged" in message.lower()


def test_invariant_still_allows_a_supersede_at_a_path_that_is_not_merged() -> None:
    """CONTROL. The first correction for a key stays legal — nothing narrows."""
    merged = _first_pass_supersede_paths()
    request = _request(merged_receipt_paths=merged)
    fresh = merged[0].replace(
        f".supersede.{_PRODUCT_PR}.yaml", f".supersede.{_SECOND_OCC_PR}.yaml"
    )
    assert fresh not in merged
    assert_append_only_emissions([_file(fresh)], request)


def test_invariant_is_a_no_op_when_the_effect_reports_no_merged_receipts() -> None:
    """CONTROL. Empty ``merged_receipt_paths`` reproduces today's behavior exactly.

    The read-EFFECT can fail to list the receipt tree (a 404, a transient API
    error). Degrading to the shipped semantics is deliberate: the pre-push
    ``_assert_append_only`` in the write-EFFECT is still fail-closed on git
    status, so a soft degrade here narrows nothing that was previously caught.
    """
    request = _request(merged_receipt_paths=())
    for path in _first_pass_supersede_paths():
        assert_append_only_emissions([_file(path)], request)


def test_invariant_still_refuses_a_plain_receipt_in_a_frozen_directory() -> None:
    """CONTROL. The OMN-15485 directory rule is untouched by the path rule."""
    request = _request(merged_receipt_paths=())
    frozen_dir = f"drift/dod_receipts/{_TICKET}/{_FIRST_ENTRY}"
    with pytest.raises(AppendOnlyEmissionError):
        assert_append_only_emissions([_file(f"{frozen_dir}/command.yaml")], request)


# ---------------------------------------------------------------------------
# AC2 — the writer: a second pass for the SAME product PR emits a NEW generation.
# ---------------------------------------------------------------------------


def test_second_pass_never_re_emits_an_already_merged_supersede_path() -> None:
    """RED. The OCC#6616 shape: pass 2 must not render pass 1's own paths."""
    merged = _first_pass_supersede_paths()
    plan = compute_companion_plan(_request(merged_receipt_paths=merged))
    collisions = _paths(plan) & set(merged)
    assert not collisions, (
        "the producer re-emitted already-merged supersede paths in place: "
        f"{sorted(collisions)}"
    )


def test_second_pass_files_the_rebind_under_a_fresh_occ_pr_generation() -> None:
    """RED. The correction is not dropped — it is filed as a net-new add."""
    merged = _first_pass_supersede_paths()
    plan = compute_companion_plan(_request(merged_receipt_paths=merged))
    supersedes = {
        f.path
        for f in plan.companion_files
        if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
    }
    assert supersedes, "pass 2 emitted no supersession at all"
    for path in supersedes:
        assert path.endswith(f".supersede.{_SECOND_OCC_PR}.yaml"), path
        assert _SUPERSEDE_SHAPE_RE.search(Path(path).name), (
            f"{path} does not match the strict .supersede.<NNNN>.yaml shape "
            "the producer's own guard and omnibase_core's resolver require"
        )


def test_fresh_generation_still_targets_this_product_pr_in_its_body() -> None:
    """The filename generation changes; the evidence binding does NOT.

    ``resolve_supersession`` tier 1 selects on the record's own
    ``replacement.pr_number``, not on the filename suffix, so re-keying the
    filename must leave the rebind pointing at the same product PR.
    """
    merged = _first_pass_supersede_paths()
    plan = compute_companion_plan(_request(merged_receipt_paths=merged))
    for companion in plan.companion_files:
        if companion.kind is not EnumCompanionFileKind.SUPERSEDE_RECEIPT:
            continue
        record = ModelReceiptSupersession.model_validate(
            yaml.safe_load(companion.content)
        )
        assert record.replacement is not None
        assert record.replacement.pr_number == _PRODUCT_PR


def test_core_resolves_the_fresh_generation_over_the_merged_one(
    tmp_path: Path,
) -> None:
    """THE CONSUMER, executed. A new generation only counts if core can see it.

    Writes BOTH the already-merged first-pass records and the producer's fresh
    second-pass output into one tree — the shape ``dev`` actually carries after
    the companion lands — and asks omnibase_core the question occ-preflight
    asks.
    """
    receipts_dir = tmp_path / "drift" / "dod_receipts"
    first = compute_companion_plan(_request())
    for companion in first.companion_files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        target = tmp_path / companion.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(companion.content, encoding="utf-8")

    merged = _first_pass_supersede_paths()
    second = compute_companion_plan(_request(merged_receipt_paths=merged))
    fresh = [
        f
        for f in second.companion_files
        if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
    ]
    assert fresh, "pass 2 emitted no supersession at all"
    for companion in fresh:
        target = tmp_path / companion.path
        target.parent.mkdir(parents=True, exist_ok=True)
        assert not target.exists(), (
            f"pass 2 would overwrite {companion.path}, which pass 1 already "
            "merged — the exact in-place rewrite OMN-16071 Defect 1 names"
        )
        target.write_text(companion.content, encoding="utf-8")

    sample = fresh[0]
    evidence_id = Path(sample.path).parent.name
    check_type = Path(sample.path).name.split(".supersede.")[0]
    resolution = resolve_supersession(
        receipts_dir,
        _TICKET,
        evidence_id,
        check_type,
        current_pr_number=_PRODUCT_PR,
    )
    assert resolution is not None, (
        "core found no supersession for the fresh generation — the producer "
        "filed the rebind under a key nothing resolves"
    )


def test_without_an_occ_pr_the_collision_is_omitted_not_rewritten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED. Pass 1 has no OCC PR number yet, so no fresh generation is derivable.

    The honest answer is then to emit nothing for that key rather than to open
    the merged file for write: the already-merged record for this SAME product
    PR carries the same ``pr_number`` / ``branch`` / ``commit_sha`` / probe
    provenance, so suppressing the re-mint loses no evidence. It must be stated,
    not silent.
    """
    merged = _first_pass_supersede_paths()
    with caplog.at_level("INFO"):
        plan = compute_companion_plan(
            _request(merged_receipt_paths=merged, occ_pr_number=None)
        )
    assert not (_paths(plan) & set(merged))
    assert "OMN-16071" in caplog.text


def test_a_partially_merged_chain_only_regenerates_the_colliding_keys() -> None:
    """Scoped to what actually collides — an untaken key keeps its natural name."""
    merged = _first_pass_supersede_paths()[:1]
    plan = compute_companion_plan(_request(merged_receipt_paths=merged))
    supersedes = sorted(
        f.path
        for f in plan.companion_files
        if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
    )
    regenerated = [p for p in supersedes if p.endswith(f".{_SECOND_OCC_PR}.yaml")]
    natural = [p for p in supersedes if p.endswith(f".{_PRODUCT_PR}.yaml")]
    assert len(regenerated) == 1, supersedes
    assert natural, "non-colliding keys must keep their product-PR-keyed name"


def test_the_skip_log_names_which_of_the_two_causes_actually_fired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_next_supersede_path`` returns ``None`` for two reasons, not one.

    Pass 1 has no OCC PR number to key a fresh generation with, and a later
    pass WILL file it. Separately, a key whose product-PR-keyed AND
    OCC-PR-keyed generations are both already merged has nothing left to add,
    and no later pass will ever emit it. The operator responses differ, so a
    log that asserts the first cause unconditionally sends a reader hunting for
    an OCC PR number that is right there, and promises a pass 2 that will never
    come. Both branches must therefore be distinguishable from the log alone.
    """
    first = _first_pass_supersede_paths()
    second = tuple(
        sorted(
            f.path
            for f in compute_companion_plan(
                _request(merged_receipt_paths=first)
            ).companion_files
            if f.kind is EnumCompanionFileKind.SUPERSEDE_RECEIPT
        )
    )
    assert second, "the regenerated pass must produce paths for this to be a test"

    with caplog.at_level("INFO"):
        exhausted = compute_companion_plan(
            _request(
                merged_receipt_paths=first + second,
                occ_pr_number=_SECOND_OCC_PR,
            )
        )
    assert not (_paths(exhausted) & set(first + second))
    exhausted_log = caplog.text
    assert "ALREADY merged" in exhausted_log, exhausted_log
    assert "no later pass will emit it" in exhausted_log, exhausted_log
    assert "no OCC PR number is available yet" not in exhausted_log, exhausted_log

    caplog.clear()
    with caplog.at_level("INFO"):
        compute_companion_plan(_request(merged_receipt_paths=first, occ_pr_number=None))
    pass_one_log = caplog.text
    assert "no OCC PR number is available yet" in pass_one_log, pass_one_log
    assert "pass 2 of this same mint files it" in pass_one_log, pass_one_log
    assert "no later pass will emit it" not in pass_one_log, pass_one_log
