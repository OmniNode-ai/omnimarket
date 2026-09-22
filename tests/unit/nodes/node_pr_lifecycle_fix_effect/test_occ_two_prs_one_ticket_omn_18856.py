# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two open product PRs under ONE ticket must mint without colliding (OMN-18856).

MEASURED DEFECT. Every receipt path the live OCC companion producer writes
encoded the product PR -- ``dod-<repo-slug>-pr-<n>``, its ``-ci`` twin, and
``occ-self-bind-pr-<occ_pr>`` -- except the final (admissibility) slot, whose id
was a bare module constant. Two open product PRs under one ticket therefore both
resolved ``drift/dod_receipts/<ticket>/dod-occ-diff-derived-behavior-proof/
<check_type>.yaml`` and both wrote it with DIFFERENT content, so whichever
companion merged second was permanently add/add CONFLICTING and no union of the
two was correct. Four occurrences on 2026-09-20: OCC#10427, OCC#10551, OCC#10560
and OCC#10571 (closed CONFLICTING/DIRTY at 16:30:08Z).

The second half of the same defect is the RECOVERY. The producer documents
"re-fires on the product PR's next lifecycle event and clones a fresh base" as
the cure for a stale companion, but the already-bound guard no-op'd on identity
alone, so a companion that had gone un-mergeable was never re-minted by anything:
the merge-heal cron reads open-and-unmergeable as "not yet" and the
preflight-heal cron re-runs a check that fails again for the same reason.

Each test here FAILS against the pre-fix emitter. Take the RED proof
path-scoped, restoring the handler directory from ``origin/dev`` and then from
``HEAD`` -- never through the shared stash stack (omni_home operating rule 17).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.models.ticket.model_contract_dod_item import (
    ModelContractDodItem,
)
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    pr_scoped_slot_evidence_id,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

_REPO = "OmniNode-ai/omnimarket"
_TICKET = "OMN-18856"

# The product diff carries a pytest target, so the BEHAVIOR item takes the slot
# -- the arm that actually collided on the four live occurrences.
_DIFF_WITH_TEST = '{"files":[{"path":"tests/unit/test_thing.py"},{"path":"src/x.py"}]}'


def _live_cap() -> int:
    """The live ``ModelContractDodItem.id`` cap, read never restated."""
    for constraint in ModelContractDodItem.model_fields["id"].metadata:
        cap = getattr(constraint, "max_length", None)
        if isinstance(cap, int):
            return cap
    raise AssertionError("ModelContractDodItem.id declares no max_length")


class _FakeTempDir:
    """``tempfile.TemporaryDirectory`` stand-in that does not delete on exit."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> None:
        return None


def _product_pr_data(*, body: str = "Implements the thing.") -> dict[str, object]:
    return {
        "body": body,
        "title": f"feat({_TICKET}): the thing",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _emit(
    tmp_root: Path,
    *,
    pr_number: int,
    repo: str = _REPO,
    occ_pr_number: int = 55,
    product_body: str = "Implements the thing.",
    occ_pr_extra: dict[str, object] | None = None,
    preseed: Callable[[Path], None] | None = None,
    diff_stdout: str = _DIFF_WITH_TEST,
) -> tuple[str, Path]:
    """Drive the REAL ``_emit_companion_sync`` against a temp OCC clone.

    Mirrors the OMN-14741 golden-gate harness: the version-control calls, the
    OCC PR open/sync, the product-PR body PATCH and the diff probe are mocked;
    contract rendering, receipt writes, the structural append and the hash
    rebind all run for real. Parameterised by ``pr_number`` because this
    suite's whole subject is two DIFFERENT product PRs resolving one ticket.
    """
    clone_root = tmp_root / "onex_change_control"

    def fake_rest(
        method: str, path: str, *, body: object = None, token: object = None
    ) -> dict[str, object]:
        if path.endswith(f"/pulls/{pr_number}"):
            return _product_pr_data(body=product_body)
        if f"/pulls/{occ_pr_number}" in path:
            slug = repo.replace("/", "-").lower()
            payload: dict[str, object] = {
                "number": occ_pr_number,
                "state": "open",
                "head": {"ref": f"auto/{slug}-pr-{pr_number}-occ-autobind"},
            }
            payload.update(occ_pr_extra or {})
            return payload
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            return "0" * 40 + "\tHEAD\n"
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        if preseed is not None:
            preseed(cd)
        return "0" * 40

    emitter = OccCompanionEmitter()
    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
        patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", return_value=occ_pr_number),
        patch.object(emitter, "_observe_pr_probe", return_value=(diff_stdout, 0)),
        # The OMN-15247 content-bound derivation reads the product repo's tree
        # over the network to prove a candidate RED against the merge base.
        # Stubbed with a derived-and-RED answer so these tests exercise the
        # BEHAVIOR arm of the slot, which is the arm all four live collisions
        # landed on; the no-candidate path is covered by its own suite.
        patch.object(
            emitter,
            "_derive_content_bound_check",
            return_value=(
                "grep -c 'def test_thing' tests/unit/test_thing.py",
                "a" * 40,
                1,
            ),
        ),
        patch.object(emitter, "_patch_evidence_source"),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_root),
        ),
    ):
        action = emitter._emit_companion_sync(repo, pr_number, _TICKET)
    return action, clone_root


def _written_paths(clone_root: Path) -> set[str]:
    return {
        str(p.relative_to(clone_root))
        for p in clone_root.rglob("*.yaml")
        if p.is_file()
    }


@pytest.fixture
def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two independent temp OCC clone roots, one per product PR's mint."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    return first, second


class TestSlotReceiptPathIsPrScoped:
    """The collision itself: one ticket, two PRs, two disjoint receipt paths."""

    def test_two_prs_under_one_ticket_never_share_a_receipt_path(
        self, two_roots: tuple[Path, Path]
    ) -> None:
        first_root, second_root = two_roots
        _action_a, clone_a = _emit(first_root, pr_number=321, occ_pr_number=55)
        _action_b, clone_b = _emit(second_root, pr_number=322, occ_pr_number=56)

        receipts_a = {p for p in _written_paths(clone_a) if "dod_receipts" in p}
        receipts_b = {p for p in _written_paths(clone_b) if "dod_receipts" in p}

        assert receipts_a, "positive control: the first mint wrote receipts at all"
        assert receipts_b, "positive control: the second mint wrote receipts at all"
        # The contract is SHARED by design and is appended to, not duplicated;
        # every RECEIPT path must be disjoint, which is what add/add needs.
        assert receipts_a.isdisjoint(receipts_b), (
            "two product PRs under one ticket wrote the same receipt path: "
            f"{sorted(receipts_a & receipts_b)}"
        )

    def test_two_repos_one_ticket_never_share_a_receipt_path(
        self, two_roots: tuple[Path, Path]
    ) -> None:
        """The CROSS-repo variant, e.g. omnimarket#2713 vs omnibase_infra#3879.

        The same-repo variant above (omnibase_core#1725 against #1726, one
        ticket, companion OCC#10560) and this one are ONE defect with one
        cause: an evidence id that named the ticket and nothing else. Both must
        come out with disjoint receipt paths.
        """
        first_root, second_root = two_roots
        _a, clone_a = _emit(
            first_root, repo="OmniNode-ai/omnimarket", pr_number=2713, occ_pr_number=55
        )
        _b, clone_b = _emit(
            second_root,
            repo="OmniNode-ai/omnibase_infra",
            pr_number=3879,
            occ_pr_number=56,
        )
        receipts_a = {p for p in _written_paths(clone_a) if "dod_receipts" in p}
        receipts_b = {p for p in _written_paths(clone_b) if "dod_receipts" in p}
        assert receipts_a, "positive control: the first mint wrote receipts"
        assert receipts_b, "positive control: the second mint wrote receipts"
        assert receipts_a.isdisjoint(receipts_b), sorted(receipts_a & receipts_b)

    def test_slot_id_embeds_the_product_pr_number(self, tmp_path: Path) -> None:
        _action, clone_root = _emit(tmp_path, pr_number=321)
        expected = pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=321
        )
        assert expected == f"{BEHAVIOR_PROOF_EVIDENCE_ID}-pr-321"
        receipt = (
            clone_root
            / "drift"
            / "dod_receipts"
            / _TICKET
            / expected
            / "test_passes.yaml"
        )
        assert receipt.is_file(), sorted(_written_paths(clone_root))
        # And the bare ticket-only path -- the one that collided -- is gone.
        assert not (
            clone_root / "drift" / "dod_receipts" / _TICKET / BEHAVIOR_PROOF_EVIDENCE_ID
        ).exists()

    def test_owed_branch_slot_id_is_scoped_too(self, tmp_path: Path) -> None:
        """A diff with no pytest target takes the admissibility arm; same rule."""
        _action, clone_root = _emit(
            tmp_path, pr_number=321, diff_stdout='{"files":[{"path":"README.md"}]}'
        )
        expected = pr_scoped_slot_evidence_id(
            ADMISSIBILITY_VALIDATOR_EVIDENCE_ID, repo=_REPO, pr_number=321
        )
        assert (
            clone_root / "drift" / "dod_receipts" / _TICKET / expected / "command.yaml"
        ).is_file(), sorted(_written_paths(clone_root))


class TestSecondCompanionDeclaresAndMintsItsOwnSlot:
    """A pre-existing contract must gain the second PR's slot item AND receipt."""

    @staticmethod
    def _preseed_from(clone_a: Path) -> Callable[[Path], None]:
        """Plant PR 321's merged companion as the second run's clone base."""

        def _seed(clone_b: Path) -> None:
            for src in clone_a.rglob("*.yaml"):
                dst = clone_b / src.relative_to(clone_a)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        return _seed

    def test_second_pr_gets_a_declared_slot_item_and_its_receipt(
        self, two_roots: tuple[Path, Path]
    ) -> None:
        first_root, second_root = two_roots
        _a, clone_a = _emit(first_root, pr_number=321, occ_pr_number=55)
        _b, clone_b = _emit(
            second_root,
            pr_number=322,
            occ_pr_number=56,
            preseed=self._preseed_from(clone_a),
        )

        contract = clone_b / "contracts" / f"{_TICKET}.yaml"
        data = yaml.safe_load(contract.read_text(encoding="utf-8"))
        ids = {item["id"] for item in data["dod_evidence"]}

        slot_321 = pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=321
        )
        slot_322 = pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo=_REPO, pr_number=322
        )
        assert slot_321 in ids, "the earlier PR's slot item was dropped"
        assert slot_322 in ids, (
            "the second companion on a shared ticket declared no slot item of "
            "its own, so it carries no behaviour proof (OMN-16434, one level down)"
        )
        assert (
            clone_b / "drift" / "dod_receipts" / _TICKET / slot_322 / "test_passes.yaml"
        ).is_file()

    def test_the_earlier_prs_merged_entry_hash_is_unchanged(
        self, two_roots: tuple[Path, Path]
    ) -> None:
        """Append-only: scoping renames nothing, so no merged entry re-hashes.

        A rename to break a same-ticket collision is the measured 2026-09-13
        defect on OCC#9327 and is refused by
        ``check_contract_change_rebinds_receipts``. This asserts the change is
        an APPEND: PR 321's own downstream entry hashes identically before and
        after PR 322's companion appends to the same contract.
        """
        first_root, second_root = two_roots
        _a, clone_a = _emit(first_root, pr_number=321, occ_pr_number=55)

        contract_a = clone_a / "contracts" / f"{_TICKET}.yaml"
        data_a: Any = yaml.safe_load(contract_a.read_text(encoding="utf-8"))
        earlier_id = "dod-OmniNode-ai-omnimarket-pr-321"
        before = compute_contract_entry_sha256(data_a, earlier_id)

        _b, clone_b = _emit(
            second_root,
            pr_number=322,
            occ_pr_number=56,
            preseed=self._preseed_from(clone_a),
        )
        contract_b = clone_b / "contracts" / f"{_TICKET}.yaml"
        data_b: Any = yaml.safe_load(contract_b.read_text(encoding="utf-8"))
        after = compute_contract_entry_sha256(data_b, earlier_id)

        assert before == after, (
            "PR 321's already-merged dod_evidence entry re-hashed when PR 322's "
            "companion appended -- that is a rewrite of merged evidence"
        )
        # Positive control: the recomputation DOES discriminate.
        mutated = yaml.safe_load(contract_b.read_text(encoding="utf-8"))
        for item in mutated["dod_evidence"]:
            if item["id"] == earlier_id:
                item["description"] = "deliberately mutated"
        assert compute_contract_entry_sha256(mutated, earlier_id) != before


class TestConflictingCompanionIsReMinted:
    """The recovery half: a bound-but-un-mergeable companion must be re-minted."""

    _BOUND_BODY = (
        "Implements the thing.\n\nEvidence-Source: OCC#55\nEvidence-Ticket: OMN-18856\n"
    )

    def test_open_and_conflicting_companion_is_re_minted_not_no_opped(
        self, tmp_path: Path
    ) -> None:
        action, clone_root = _emit(
            tmp_path,
            pr_number=321,
            occ_pr_number=55,
            product_body=self._BOUND_BODY,
            occ_pr_extra={"mergeable": False, "mergeable_state": "dirty"},
        )
        assert "no-op" not in action, action
        assert (clone_root / "contracts" / f"{_TICKET}.yaml").is_file(), (
            "a conflicting companion was not re-minted, so nothing repaired it"
        )

    def test_healthy_bound_companion_still_no_ops(self, tmp_path: Path) -> None:
        """Positive control for the guard: mergeable True keeps the no-op."""
        action, _clone_root = _emit(
            tmp_path,
            pr_number=321,
            occ_pr_number=55,
            product_body=self._BOUND_BODY,
            occ_pr_extra={"mergeable": True, "mergeable_state": "clean"},
        )
        assert action.startswith("no-op:"), action

    def test_indeterminate_mergeability_fails_closed_to_the_no_op(
        self, tmp_path: Path
    ) -> None:
        """GitHub returns null while computing; a guess must not force-push."""
        action, _clone_root = _emit(
            tmp_path,
            pr_number=321,
            occ_pr_number=55,
            product_body=self._BOUND_BODY,
            occ_pr_extra={"mergeable": None, "mergeable_state": "unknown"},
        )
        assert action.startswith("no-op:"), action

    def test_merged_companion_is_never_re_minted(self, tmp_path: Path) -> None:
        """A finished companion is finished; only OPEN is re-mintable."""
        action, _clone_root = _emit(
            tmp_path,
            pr_number=321,
            occ_pr_number=55,
            product_body=self._BOUND_BODY,
            occ_pr_extra={"state": "closed", "merged": True, "mergeable": False},
        )
        assert action.startswith("no-op:"), action


class TestScopedIdFitsTheContractSchemaCap:
    """The scoping suffix is sized by a hard schema cap, not by preference."""

    def test_both_scoped_ids_fit_the_live_cap_at_six_digit_pr_numbers(self) -> None:
        cap = _live_cap()
        for base in (BEHAVIOR_PROOF_EVIDENCE_ID, ADMISSIBILITY_VALIDATOR_EVIDENCE_ID):
            scoped = pr_scoped_slot_evidence_id(base, repo=_REPO, pr_number=999999)
            assert len(scoped) <= cap, f"{scoped!r} is {len(scoped)} of {cap}"

    def test_a_repo_qualified_suffix_would_not_have_fit(self) -> None:
        """Why the suffix is ``-pr-<n>`` and not ``-<repo-slug>-pr-<n>``.

        This is the measurement behind that choice, kept executable so a later
        reader does not re-propose the longer form and mint a corpus-wide
        unvalidatable contract.
        """
        would_be = (
            f"{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}-OmniNode-ai-omnimarket-pr-2713"
        )
        assert len(would_be) > _live_cap()

    def test_an_over_long_id_raises_rather_than_minting_silently(self) -> None:
        with pytest.raises(ValueError, match="caps at"):
            pr_scoped_slot_evidence_id(
                ADMISSIBILITY_VALIDATOR_EVIDENCE_ID, repo=_REPO, pr_number=12345678901
            )


class TestKnownResidual:
    """Stated, pinned, and deliberately not silently closed."""

    def test_identical_pr_numbers_in_two_repos_still_share_the_id(self) -> None:
        """The one case PR-number scoping does NOT separate.

        Never observed; the case it closes was observed four times in one day.
        Closing it needs a shorter base id, which is a RENAME of ids already
        merged across the corpus -- the move ``check_contract_change_rebinds_
        receipts`` refuses. Pinned here so the limit is a recorded decision
        rather than a surprise to the next reader.
        """
        a = pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo="OmniNode-ai/omnimarket", pr_number=2286
        )
        b = pr_scoped_slot_evidence_id(
            BEHAVIOR_PROOF_EVIDENCE_ID, repo="OmniNode-ai/omniclaude", pr_number=2286
        )
        assert a == b
