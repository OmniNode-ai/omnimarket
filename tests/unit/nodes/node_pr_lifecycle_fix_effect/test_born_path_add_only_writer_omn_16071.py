# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16071 Defect 1, born path: the slot receipt guard must read the PATH.

``OccCompanionEmitter`` skips the ticket-scoped slot receipt on the strength of

    contract_already_had_companion[ticket] = contract_path.is_file()

which is a question about ``contracts/<ticket>.yaml`` used to decide whether to
open ``drift/dod_receipts/<ticket>/<slot_evidence_id>/<slot_check_type>.yaml``
for write. Two different files. Its sibling emission sites in the same loop ask
the direct question — ``downstream_receipt_path.is_file()`` and
``ci_receipt_path.is_file()`` — and this one never did.

WHY THE PROXY IS NOT MERELY UNTIDY. The ticket's AC is literal: *"the autobind
path must be strictly add-only — never open an existing receipt file for
write."* A guard keyed to a different file cannot deliver that property; it
delivers it only while the two files happen to co-exist. When they diverge —
a receipt tree that outlived its contract, a contract renamed or re-keyed to a
different ticket (OMN-16376's wrong-ticket-keying defect is exactly that
divergence, filed separately) — the writer opens an already-merged receipt.
Since OMN-16071's own PR #2086 the pre-push ``_assert_append_only`` then aborts
the ENTIRE mint on git status, so the product PR gets no companion at all.

The fix is one boolean per ticket: skip when the receipt path itself is
already present at the clone base. OMN-16071 landed it as a disjunction —
that, **or** the contract pre-existed — and kept the contract half because
minting a slot receipt into a pre-existing contract that did not declare that
item would have traded an append-only violation for an orphan receipt.

AMENDED 2026-09-20 (OMN-18856): the contract half is GONE, and its removal is
the point rather than a tidy-up. It was sound only while the slot id was
ticket-shared and therefore undeclarable by a second companion; now that the
id is ``<base>-pr-<n>``, ``_ensure_base_dod_evidence`` appends THIS PR's slot
item to a pre-existing contract, so the orphan-receipt hazard it guarded no
longer exists — while keeping it would have meant every second-and-later
companion on a shared ticket silently carried no behaviour proof at all. What
remains is the honest predicate this module was written to demand: never open
an existing receipt FILE for write.

RED-before, against ``dev`` @ ``482648e1``: the emitter leg below drives the
REAL ``_emit_companion_sync`` over a clone that already carries the merged
receipt and observes its bytes change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    pr_scoped_slot_evidence_id,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
_TICKET = "OMN-16071"
_REPO = "OmniNode-ai/omninode_infra"
_PR = 906
_OCC_PR = 55

# OMN-18856: the slot receipt this module is about now lands under a PR-SCOPED
# id. Derived with the producer's own helper, never restated as a literal, so
# the fixture cannot come to describe a path the producer does not write.
_SLOT_ID = pr_scoped_slot_evidence_id(
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID, repo=_REPO, pr_number=_PR
)


def _merged_receipt_body(evidence_item_id: str) -> str:
    """An already-merged slot receipt, verbatim in shape from the ticket body.

    The bytes are the ones OMN-16071 quoted — PR #900's companion, which the
    autobind run for #906 then rewrote with #906's probe values. Only the
    ``evidence_item_id`` is parameterised, so one fixture can stand both for a
    receipt merged under TODAY'S scoped id and for one merged under the
    ticket-shared id that preceded OMN-18856.
    """
    return f"""---
schema_version: "1.0.0"
ticket_id: "OMN-16071"
evidence_item_id: "{evidence_item_id}"
check_type: "command"
check_value: "uv run pytest tests/test_evidence_admissibility.py"
status: "PASS"
run_timestamp: "2026-08-14T01:45:54.147996+00:00"
commit_sha: "d8532c979ddff64b3d80deca4296e44cc42e1b18"
runner: "occ-companion-manual"
verifier: "occ-evidence-source-bind"
probe_command: "gh pr view 900 --repo OmniNode-ai/omninode_infra --json number,state"
probe_stdout: |
  {{"number":900,"state":"OPEN"}}
exit_code: 0
pr_number: 900
"""


# FIXTURE CHOICE (OMN-18856), stated because it decides what these legs prove:
# the receipt the add-only guard must refuse to reopen is seeded under the
# SCOPED id. Today's writer targets that path and only that path, so seeding
# the old unscoped one would leave every leg below vacuously green while
# testing nothing at all. The pre-OMN-18856 id is still exercised, as the
# historical artifact it is, by the dedicated leg at the bottom of this module.
_MERGED_RECEIPT_BODY = _merged_receipt_body(_SLOT_ID)

# The same receipt as it was minted BEFORE the id was scoped: a real,
# already-merged artifact still sitting in the OCC corpus at the unscoped path.
_LEGACY_RECEIPT_BODY = _merged_receipt_body(ADMISSIBILITY_VALIDATOR_EVIDENCE_ID)


class _FakeTempDir:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the legacy binding so the mint does not take the fail-closed branch."""
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


def _pr_data() -> dict[str, object]:
    return {
        "body": f"Closes {_TICKET}",
        "title": f"fix({_TICKET}): autobind receipt writer is add-only",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _drive_emit(
    tmp_path: Path,
    *,
    seed_contract: bool,
    seed_receipt: bool,
    seed_legacy_receipt: bool = False,
) -> tuple[str, Path, Path]:
    """Run the REAL ``_emit_companion_sync`` against a pre-seeded temp clone.

    ``seed_contract`` / ``seed_receipt`` reproduce the two files the guard this
    module is about used to conflate; ``seed_legacy_receipt`` additionally
    plants a receipt at the pre-OMN-18856 unscoped path. The version-control
    calls, the OCC-PR open and the product-PR read are mocked; the contract and
    receipt rendering, the file writes and the rebind pass all run for real, so
    what this observes is what the live producer would push.
    """
    emitter = OccCompanionEmitter()
    clone_root = tmp_path / "onex_change_control"
    receipts_root = clone_root / "drift" / "dod_receipts" / _TICKET
    receipt_path = receipts_root / _SLOT_ID / "command.yaml"
    legacy_receipt_path = (
        receipts_root / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID / "command.yaml"
    )

    def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
        if path.endswith(f"/pulls/{_PR}"):
            return _pr_data()
        if f"/pulls/{_OCC_PR}" in path:
            return {"number": _OCC_PR, "state": "open"}
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        if "rev-parse" in argv:
            return "c" * 40
        if "ls-remote" in argv:
            return "0" * 40 + "\tHEAD\n"
        return ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        if seed_contract:
            contract_dir = cd / "contracts"
            contract_dir.mkdir(parents=True, exist_ok=True)
            # Declares the SCOPED id: a contract already carrying THIS PR's
            # slot item is what a re-fire on the same product PR observes, and
            # it is the state OMN-18856 made reachable. An unscoped item here
            # would model a shape no producer mints any more.
            (contract_dir / f"{_TICKET}.yaml").write_text(
                '---\nschema_version: "1.0.0"\nticket_id: '
                f'"{_TICKET}"\ndod_evidence:\n'
                f'  - id: "{_SLOT_ID}"\n'
                '    description: "prior companion"\n'
                '    checks:\n      - check_type: "command"\n'
                '        check_value: "uv run pytest tests/test_evidence_admissibility.py"\n',
                encoding="utf-8",
            )
        if seed_receipt:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(_MERGED_RECEIPT_BODY, encoding="utf-8")
        if seed_legacy_receipt:
            legacy_receipt_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_receipt_path.write_text(_LEGACY_RECEIPT_BODY, encoding="utf-8")
        return "0" * 40

    def fake_probe(
        *, probe_command: str, token: str, fallback: dict
    ) -> tuple[str, int]:
        if "--json files" in probe_command:
            return '{"files":[]}', 0
        return f'{{"number":{_PR},"state":"open"}}', 0

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
        patch(f"{_MOD}.release_occ_companion_lease", MagicMock()),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", return_value=_OCC_PR),
        patch.object(emitter, "_observe_pr_probe", side_effect=fake_probe),
        patch.object(emitter, "_patch_evidence_source"),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ):
        action = emitter._emit_companion_sync(_REPO, _PR, None)
    return action, receipt_path, legacy_receipt_path


def test_a_merged_receipt_survives_a_mint_whose_contract_is_absent(
    tmp_path: Path,
) -> None:
    """RED. The proxy's blind spot, driven through the real writer.

    Contract absent, receipt present: the pre-OMN-16071 guard read
    ``contract_path.is_file() is False``, concluded the ticket had no prior
    companion, and opened an already-merged receipt for write with this run's
    probe values — the diff quoted verbatim in this ticket's body. Only the
    receipt-path half can refuse that, and since OMN-18856 it is the only half
    there is.
    """
    _action, receipt_path, _legacy = _drive_emit(
        tmp_path, seed_contract=False, seed_receipt=True
    )
    assert receipt_path.is_file()
    assert receipt_path.read_text(encoding="utf-8") == _MERGED_RECEIPT_BODY, (
        "the born-path writer opened an already-merged receipt for write — "
        "OMN-16071 Defect 1"
    )


def test_the_receipt_guard_holds_when_the_contract_pre_exists_too(
    tmp_path: Path,
) -> None:
    """CONTROL. Contract AND receipt present is still skipped.

    REWRITTEN 2026-09-20 (OMN-18856), and the rename is the finding. This leg
    was ``test_the_omn_15785_contract_guard_is_retained`` and its claim —
    that a pre-existing CONTRACT is itself a reason to skip the slot receipt —
    is no longer true of the producer: that half of the disjunction was
    deliberately removed, because with a PR-scoped id the second companion on
    a shared ticket declares and owes its own slot item. The assertion is
    unchanged and still load-bearing under the surviving half: when the
    receipt FILE is already there, it is left byte-for-byte alone whether or
    not a contract sits beside it.
    """
    _action, receipt_path, _legacy = _drive_emit(
        tmp_path, seed_contract=True, seed_receipt=True
    )
    assert receipt_path.read_text(encoding="utf-8") == _MERGED_RECEIPT_BODY


def test_a_fresh_ticket_still_mints_its_slot_receipt(tmp_path: Path) -> None:
    """CONTROL. Neither file present is the born case — the mint must still happen.

    Without this the fix could pass leg 1 by never writing the slot receipt at
    all, which trades the append-only violation for a born-INELIGIBLE companion
    (``MISSING_RECEIPT``, OMN-15247 R21b).
    """
    _action, receipt_path, _legacy = _drive_emit(
        tmp_path, seed_contract=False, seed_receipt=False
    )
    assert receipt_path.is_file(), "fresh ticket minted no slot receipt"
    body = receipt_path.read_text(encoding="utf-8")
    assert f"pr_number: {_PR}" in body
    assert body != _MERGED_RECEIPT_BODY


def test_a_historical_unscoped_receipt_is_never_reopened(tmp_path: Path) -> None:
    """The corpus half of OMN-18856: receipts merged before the id was scoped.

    Every slot receipt merged before 2026-09-20 sits at
    ``drift/dod_receipts/<ticket>/<base>/<check_type>.yaml`` with the bare id
    inside it. Scoping deliberately does NOT rename those — renaming an
    evidence item to break a collision is the measured 2026-09-13 defect on
    OCC#9327 and ``check_contract_change_rebinds_receipts`` refuses it — so
    they must simply be left where they are. This leg plants one and drives a
    full mint over it: the run writes its own scoped receipt and does not
    touch, rebind or rewrite the historical file.
    """
    _action, receipt_path, legacy_receipt_path = _drive_emit(
        tmp_path,
        seed_contract=False,
        seed_receipt=False,
        seed_legacy_receipt=True,
    )
    assert legacy_receipt_path.read_text(encoding="utf-8") == _LEGACY_RECEIPT_BODY, (
        "a mint reopened a receipt merged under the pre-OMN-18856 unscoped id"
    )
    assert receipt_path.is_file(), (
        "the run minted no slot receipt of its own — a historical unscoped "
        "receipt must not suppress this PR's own evidence"
    )
