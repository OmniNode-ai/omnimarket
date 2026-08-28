# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16892: the BORN-PATH OCC producer must mint diff-derived behavior proof.

Defect this pins (measured on the live OCC corpus, 2026-08-28). ``omnimarket``
ships TWO OCC-companion producers, and OMN-16434 hardened exactly one of them:

  * ``node_occ_companion_compute`` (the compute oracle) renders through
    ``_COMPUTE_CONTRACT_HEAD_TEMPLATE`` and, since OMN-16434, derives a
    BEHAVIOR-class check from the product PR's own diff.
  * ``OccCompanionEmitter`` (this module's subject — the born path, OMN-13317
    F1) renders through :func:`render_companion_contract`, which took no
    ``changed_files`` argument at all. It therefore could not call
    ``derive_behavior_test_paths``, could not reach
    ``render_behavior_proof_dod_evidence_item``, and unconditionally minted the
    ticket-independent ``dod-occ-evidence-admissibility-validator`` surrogate
    into the slot the behavior item belongs in.

The consequence is structural, not incidental: EVERY born-path companion was
born at ``behavior_proving_count == 0``, and the OMN-16821 autoclose flip
predicate requires ``behavior_proving_count > 0``. Of the 22 OCC contracts
created after omnimarket#2180 landed, 8 came from this producer and every one
of them carries zero behavior checks.

``contracts/OMN-16599.yaml`` is the clean falsification and the case this
module reproduces: omnimarket#2187's diff ADDS
``tests/unit/nodes/node_emit_daemon/test_fanout_partial_drop_omn16599.py`` — a
perfectly derivable pytest target — and the born path minted a ``?ref=``-pinned
``grep -c`` CONTENT READ of that exact test file instead of running it.

Proof structure (feedback_prove_red_against_exists_but_wrong):
  * GREEN — a PR whose diff carries a pytest target yields a contract with a
    BEHAVIOR-class item bound to that exact target at the product repo's
    ``cwd``, and the ticket-independent foreign suite is GONE.
  * RED control — the SAME producer on the SAME PR with the test file removed
    from the diff mints NO behavior item at all. That contrast is what proves
    the check is derived from the diff rather than minted from a fixed corpus.
  * Honesty leg — on that no-test-change PR the producer states what proof is
    OWED in ``evidence_requirements`` rather than minting a surrogate that
    reads as proof.
  * Receipt leg (OMN-16859 constraint) — the emitter mints a receipt whose
    FILENAME matches the ``check_type`` the contract declares, and the
    append-only guard permits exactly that path. Declaring ``test_passes`` and
    minting only ``command.yaml`` is the OMN-16859 defect; this producer must
    not reproduce it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_item_checks,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    behavior_proof_cwd,
    render_companion_contract,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

_TICKET = "OMN-16892"
_REPO = "OmniNode-ai/omnimarket"
_PR = 321
_EVIDENCE_ID = f"dod-{_REPO.replace('/', '-')}-pr-{_PR}"

# The OMN-16599 shape, verbatim in kind: one source file and one pytest target.
_SRC_FILE = "src/omnimarket/nodes/node_emit_daemon/handlers/handler_emit_daemon.py"
_TEST_FILE = "tests/unit/nodes/node_emit_daemon/test_fanout_partial_drop_omn16599.py"

# A path under tests/ that is NOT a pytest collection target. Naming it must not
# mint a behavior check: a command that collects nothing passes vacuously, which
# is the exact class of check this ticket exists to remove.
_NON_TARGET = "tests/unit/nodes/node_emit_daemon/conftest.py"


# ---------------------------------------------------------------------------
# Pure renderer — the seam both the born path and its tests share
# ---------------------------------------------------------------------------


def _render(changed: tuple[str, ...]) -> dict[str, object]:
    raw = render_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_PR,
        evidence_id=_EVIDENCE_ID,
        changed_files=changed,
    )
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), "producer emitted non-mapping YAML"
    return parsed


def _items(contract: dict[str, object]) -> list[dict[str, object]]:
    dod = contract.get("dod_evidence")
    assert isinstance(dod, list)
    return [item for item in dod if isinstance(item, dict)]


def _behavior_proving_count(contract: dict[str, object]) -> int:
    """The OMN-15911 conjunct, computed exactly as node_dod_verify computes it.

    Deliberately NOT a string match on the minted YAML: the number the autoclose
    flip predicate reads is produced by ``classify_item_checks``, so that is the
    function this gate must call. A test that greps for ``pytest`` would pass on
    a check the real classifier rejects.
    """
    return sum(
        1
        for item in _items(contract)
        if classify_item_checks(item.get("checks") or [])
        is EnumCheckProofClass.BEHAVIOR
    )


def _item_by_id(contract: dict[str, object], item_id: str) -> dict[str, object] | None:
    return next((i for i in _items(contract) if i.get("id") == item_id), None)


def _check_values(contract: dict[str, object]) -> list[str]:
    values: list[str] = []
    for item in _items(contract):
        for check in item.get("checks") or []:
            value = check.get("check_value")
            if isinstance(value, str):
                values.append(value)
    return values


def _tests_requirement(contract: dict[str, object]) -> dict[str, object] | None:
    reqs = contract.get("evidence_requirements")
    assert isinstance(reqs, list)
    return next(
        (r for r in reqs if isinstance(r, dict) and r.get("kind") == "tests"),
        None,
    )


class TestBornPathDerivesBehaviorProof:
    """GREEN: a diff carrying a pytest target yields a BEHAVIOR-class check."""

    def test_behavior_proving_count_is_at_least_one(self) -> None:
        contract = _render((_SRC_FILE, _TEST_FILE))
        assert _behavior_proving_count(contract) >= 1, (
            "born-path companion carries no BEHAVIOR-class check even though the "
            "PR diff adds a pytest target — the OMN-16821 flip predicate can "
            "never be satisfied for this ticket"
        )

    def test_the_behavior_item_names_the_exact_diff_target(self) -> None:
        contract = _render((_SRC_FILE, _TEST_FILE))
        item = _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID)
        assert item is not None, "no diff-derived behavior item was minted"
        checks = item.get("checks")
        assert isinstance(checks, list)
        assert len(checks) == 1
        check = checks[0]
        assert check["check_type"] == "test_passes"
        assert _TEST_FILE in check["check_value"]
        # The source file is not a collection target and must not be named.
        assert _SRC_FILE not in check["check_value"]
        # Runs in the PRODUCT repo, not wherever the verifier happens to stand.
        assert check["cwd"] == behavior_proof_cwd(_REPO)

    def test_the_ticket_independent_surrogate_is_not_also_minted(self) -> None:
        """One slot, one occupant.

        Minting both would put a check that classifies SURROGATE next to one
        that classifies BEHAVIOR on the same contract, which reads as two
        proofs and is one — and the surrogate is on the foreign-suite denylist
        precisely because its exit status cannot depend on this ticket's diff.
        """
        contract = _render((_SRC_FILE, _TEST_FILE))
        assert _item_by_id(contract, ADMISSIBILITY_VALIDATOR_EVIDENCE_ID) is None
        assert ADMISSIBILITY_VALIDATOR_CHECK_VALUE not in _check_values(contract)

    def test_the_stated_requirement_matches_the_executed_check(self) -> None:
        """One derivation, two consumers, no drift.

        ``evidence_requirements`` is what the contract SAYS it requires and the
        dod_evidence item is what a verifier RUNS. A contract whose stated bar
        and executed check disagree is unauditable regardless of which one is
        right.
        """
        contract = _render((_SRC_FILE, _TEST_FILE))
        requirement = _tests_requirement(contract)
        assert requirement is not None, "no tests-kind evidence requirement declared"
        item = _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID)
        assert item is not None
        assert requirement["command"] == item["checks"][0]["check_value"]


class TestBornPathRedControl:
    """RED control: no derivable target ⇒ no behavior item, and it says so."""

    def test_no_behavior_item_when_the_diff_carries_no_pytest_target(self) -> None:
        contract = _render((_SRC_FILE,))
        assert _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID) is None
        assert _behavior_proving_count(contract) == 0

    def test_a_non_collectable_path_under_tests_is_not_a_target(self) -> None:
        """``tests/conftest.py`` is not a pytest target.

        Naming it would mint a command that collects nothing and exits 0 — a
        vacuous green, which is strictly worse than an honest zero.
        """
        contract = _render((_SRC_FILE, _NON_TARGET))
        assert _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID) is None
        assert _behavior_proving_count(contract) == 0

    def test_the_unmet_bar_is_stated_rather_than_papered_over(self) -> None:
        contract = _render((_SRC_FILE,))
        requirement = _tests_requirement(contract)
        assert requirement is not None, (
            "the producer minted no behavior check AND said nothing about what is "
            "owed — the shortfall is invisible to any reader of this contract"
        )
        assert "OWED" in str(requirement["description"])
        assert _SRC_FILE in str(requirement["description"])

    def test_the_admissibility_floor_is_retained_when_nothing_is_derivable(
        self,
    ) -> None:
        """Dropping the floor here would trade one defect for a worse one.

        ``contract_compliance_check`` exits 1 with "no hosted-and-local
        effective check exists" on a contract carrying only ``gh pr view``
        provenance, and a companion born BLOCKED wedges the product PR behind
        it. A visibly-labelled surrogate is the lesser failure; the honest
        statement of the gap lives in ``evidence_requirements`` above.
        """
        contract = _render((_SRC_FILE,))
        assert _item_by_id(contract, ADMISSIBILITY_VALIDATOR_EVIDENCE_ID) is not None


# ---------------------------------------------------------------------------
# Emitter leg — the REAL OccCompanionEmitter, not the renderer in isolation
# ---------------------------------------------------------------------------


class _FakeTempDir:
    """``tempfile.TemporaryDirectory`` stand-in that does not delete on exit."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the legacy binding so the mint does not take the fail-closed branch.

    Same rationale as ``test_occ_companion_emitter_friction_omn_14741``: this
    module asserts properties that are binding-orthogonal, and under the
    ``content_bound`` default an un-faked RED derivation correctly aborts the
    mint before any contract is rendered.
    """
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


def _pr_data() -> dict[str, object]:
    return {
        "body": f"Closes {_TICKET}",
        "title": f"fix({_TICKET}): born path mints diff-derived behavior proof",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _drive_emit(tmp_path: Path, *, changed_files: tuple[str, ...]) -> tuple[str, Path]:
    """Run the REAL ``_emit_companion_sync`` against a temp clone.

    git, the OCC-PR open, and the product-PR read are mocked; the contract and
    receipt rendering, the file writes, the rebind pass and the append-only
    guard all run for real, so the byte shape this asserts is the byte shape the
    live producer pushes.

    ``changed_files`` is injected through the ONE observation the producer
    already makes — the ``gh pr view <n> --json files`` diff-scope probe — and
    not through a new seam invented for the test. That is deliberate: a test
    that fed the file list in by a private back door would pass even if the
    production plumbing were wired to nothing.
    """
    emitter = OccCompanionEmitter()
    clone_root = tmp_path / "onex_change_control"

    def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
        if path.endswith(f"/pulls/{_PR}"):
            return _pr_data()
        if "/pulls/55" in path:
            return {"number": 55, "state": "open"}
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            return "0" * 40 + "\tHEAD\n"
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        return "0" * 40

    def fake_probe(
        *, probe_command: str, token: str, fallback: dict
    ) -> tuple[str, int]:
        if "--json files" in probe_command:
            payload = ",".join(f'{{"path":"{p}"}}' for p in changed_files)
            return f'{{"files":[{payload}]}}', 0
        return f'{{"number":{_PR},"state":"open"}}', 0

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
        patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
        patch.object(emitter, "_observe_pr_probe", side_effect=fake_probe),
        patch.object(emitter, "_patch_evidence_source"),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ):
        action = emitter._emit_companion_sync(_REPO, _PR, None)
    return action, clone_root


def _emitted_contract(clone_root: Path) -> dict[str, object]:
    path = clone_root / "contracts" / f"{_TICKET}.yaml"
    assert path.is_file(), f"producer wrote no contract at {path}"
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


class TestEmitterWiresTheDiffIntoTheContract:
    """The plumbing leg: the live emitter, not the renderer called by hand."""

    def test_live_emit_carries_a_behavior_proving_check(self, tmp_path: Path) -> None:
        _action, clone_root = _drive_emit(
            tmp_path, changed_files=(_SRC_FILE, _TEST_FILE)
        )
        contract = _emitted_contract(clone_root)
        assert _behavior_proving_count(contract) >= 1
        item = _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID)
        assert item is not None
        assert _TEST_FILE in item["checks"][0]["check_value"]

    def test_receipt_filename_matches_the_declared_check_type(
        self, tmp_path: Path
    ) -> None:
        """The OMN-16859 constraint, applied to this producer at birth.

        ``validator_occ_merge_eligibility`` resolves an item's receipt by the
        ``check_type`` the contract declares. Declaring ``test_passes`` while
        minting only ``command.yaml`` yields MISSING_RECEIPT and a companion
        born INELIGIBLE — which is how the compute producer currently behaves
        and why its behavior receipts are hand-authored. This producer mints
        the matching name itself.
        """
        _action, clone_root = _drive_emit(
            tmp_path, changed_files=(_SRC_FILE, _TEST_FILE)
        )
        contract = _emitted_contract(clone_root)
        item = _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID)
        assert item is not None
        declared_type = item["checks"][0]["check_type"]

        receipt_dir = (
            clone_root / "drift" / "dod_receipts" / _TICKET / BEHAVIOR_PROOF_EVIDENCE_ID
        )
        expected = receipt_dir / f"{declared_type}.yaml"
        assert expected.is_file(), (
            f"contract declares check_type {declared_type!r} but the producer "
            f"minted no {expected.name} — the item resolves to MISSING_RECEIPT"
        )
        receipt = yaml.safe_load(expected.read_text(encoding="utf-8"))
        assert receipt["check_type"] == declared_type
        assert receipt["evidence_item_id"] == BEHAVIOR_PROOF_EVIDENCE_ID
        assert receipt["status"] == "PASS"

    def test_no_orphan_surrogate_receipt_is_minted_alongside(
        self, tmp_path: Path
    ) -> None:
        """Exactly one slot receipt, matching the item the contract carries.

        Minting the admissibility receipt too would declare a receipt for an
        item the contract does not carry, which ``check_receipt_hardening``
        reports as an orphan.
        """
        _action, clone_root = _drive_emit(
            tmp_path, changed_files=(_SRC_FILE, _TEST_FILE)
        )
        orphan = (
            clone_root
            / "drift"
            / "dod_receipts"
            / _TICKET
            / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
        )
        assert not orphan.exists()

    def test_the_owed_branch_still_mints_the_admissibility_receipt(
        self, tmp_path: Path
    ) -> None:
        """The RED control at the emitter level, receipts included."""
        _action, clone_root = _drive_emit(tmp_path, changed_files=(_SRC_FILE,))
        contract = _emitted_contract(clone_root)
        assert _item_by_id(contract, BEHAVIOR_PROOF_EVIDENCE_ID) is None
        assert (
            clone_root
            / "drift"
            / "dod_receipts"
            / _TICKET
            / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
            / "command.yaml"
        ).is_file()
        assert not (
            clone_root / "drift" / "dod_receipts" / _TICKET / BEHAVIOR_PROOF_EVIDENCE_ID
        ).exists()

    def test_the_mint_is_not_refused_by_its_own_append_only_guard(
        self, tmp_path: Path
    ) -> None:
        """``_assert_append_only`` must permit the new receipt path.

        The guard is fail-closed on any path outside the run's allowed set, so
        a behavior receipt written without widening ``_allowed_paths`` aborts
        the whole mint. Asserting the ACTION rather than the file is what makes
        this leg falsifiable: a forgotten allowed-path entry surfaces here as a
        refusal, not as a silently missing file.
        """
        action, _clone_root = _drive_emit(
            tmp_path, changed_files=(_SRC_FILE, _TEST_FILE)
        )
        assert not action.startswith("skip:"), action
        assert "append-only" not in action.lower()


def _contract_dod_item_id_max_length() -> int:
    """Read the live schema cap instead of restating it as a literal.

    A hardcoded 50 here would keep passing after the schema moved, which is the
    failure mode this pin exists to prevent.
    """
    for constraint in ModelContractDodItem.model_fields["id"].metadata:
        cap = getattr(constraint, "max_length", None)
        if isinstance(cap, int):
            return cap
    raise AssertionError(
        "ModelContractDodItem.id declares no max_length constraint; the cap "
        "this pin defends was removed or moved -- re-derive it."
    )


class TestMintedEvidenceIdFitsTheContractSchemaCap:
    """OMN-16434's un-landed residual, folded in per its closing comment.

    ``ModelContractDodItem.id`` is ``max_length=50`` and pydantic rejects the
    WHOLE contract when one id exceeds it — measured live when OCC#7384's first
    hand-authored id was refused. ``BEHAVIOR_PROOF_EVIDENCE_ID`` is a module
    constant now written by BOTH producers, so a rename past that cap would not
    fail one contract; it would make every companion either producer mints
    unvalidatable, silently, at the next mint. One assertion converts that into
    a failing unit test.
    """

    def test_the_behavior_proof_evidence_id_fits_the_contract_schema_cap(
        self,
    ) -> None:
        cap = _contract_dod_item_id_max_length()
        assert cap == 50, (
            "the cap this pin defends moved; re-derive it rather than "
            f"loosening the assertion (now {cap})"
        )
        assert len(BEHAVIOR_PROOF_EVIDENCE_ID) <= cap, (
            f"{BEHAVIOR_PROOF_EVIDENCE_ID!r} is "
            f"{len(BEHAVIOR_PROOF_EVIDENCE_ID)} chars; ModelContractDodItem "
            f"rejects an id over {cap}, which would make every companion "
            "either producer mints unvalidatable"
        )

    def test_the_admissibility_evidence_id_fits_the_same_cap(self) -> None:
        """The OWED branch's id travels the identical path and same schema."""
        assert (
            len(ADMISSIBILITY_VALIDATOR_EVIDENCE_ID)
            <= _contract_dod_item_id_max_length()
        )
