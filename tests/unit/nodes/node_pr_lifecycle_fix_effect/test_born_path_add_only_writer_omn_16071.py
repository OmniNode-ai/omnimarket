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

The fix is one boolean per ticket: skip when the contract pre-existed **or**
when the receipt path itself is already present at the clone base. Strictly
safer than today in both directions and it keeps OMN-15785's guard intact —
the contract half of the condition is retained deliberately, because minting a
slot receipt into a pre-existing contract that does not declare that item would
trade an append-only violation for an orphan receipt.

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
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
_TICKET = "OMN-16071"
_REPO = "OmniNode-ai/omninode_infra"
_PR = 906
_OCC_PR = 55

# Verbatim from the ticket body: the merged receipt PR #900's companion landed,
# which the autobind run for #906 then rewrote with #906's probe values.
_MERGED_RECEIPT_BODY = """---
schema_version: "1.0.0"
ticket_id: "OMN-16071"
evidence_item_id: "dod-occ-evidence-admissibility-validator"
check_type: "command"
check_value: "uv run pytest tests/test_evidence_admissibility.py"
status: "PASS"
run_timestamp: "2026-08-14T01:45:54.147996+00:00"
commit_sha: "d8532c979ddff64b3d80deca4296e44cc42e1b18"
runner: "occ-companion-manual"
verifier: "occ-evidence-source-bind"
probe_command: "gh pr view 900 --repo OmniNode-ai/omninode_infra --json number,state"
probe_stdout: |
  {"number":900,"state":"OPEN"}
exit_code: 0
pr_number: 900
"""


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
) -> tuple[str, Path]:
    """Run the REAL ``_emit_companion_sync`` against a pre-seeded temp clone.

    ``seed_contract`` / ``seed_receipt`` reproduce the two files the live guard
    conflates. git, the OCC-PR open and the product-PR read are mocked; the
    contract and receipt rendering, the file writes and the rebind pass all run
    for real, so what this observes is what the live producer would push.
    """
    emitter = OccCompanionEmitter()
    clone_root = tmp_path / "onex_change_control"
    receipt_path = (
        clone_root
        / "drift"
        / "dod_receipts"
        / _TICKET
        / ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
        / "command.yaml"
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
            (contract_dir / f"{_TICKET}.yaml").write_text(
                '---\nschema_version: "1.0.0"\nticket_id: '
                f'"{_TICKET}"\ndod_evidence:\n'
                f'  - id: "{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}"\n'
                '    description: "prior companion"\n'
                '    checks:\n      - check_type: "command"\n'
                '        check_value: "uv run pytest tests/test_evidence_admissibility.py"\n',
                encoding="utf-8",
            )
        if seed_receipt:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(_MERGED_RECEIPT_BODY, encoding="utf-8")
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
    return action, receipt_path


def test_a_merged_receipt_survives_a_mint_whose_contract_is_absent(
    tmp_path: Path,
) -> None:
    """RED. The proxy's blind spot, driven through the real writer.

    Contract absent, receipt present: the shipped guard reads
    ``contract_path.is_file() is False``, concludes the ticket has no prior
    companion, and opens PR #900's already-merged receipt for write with #906's
    probe values — the diff quoted verbatim in this ticket's body.
    """
    _action, receipt_path = _drive_emit(
        tmp_path, seed_contract=False, seed_receipt=True
    )
    assert receipt_path.is_file()
    assert receipt_path.read_text(encoding="utf-8") == _MERGED_RECEIPT_BODY, (
        "the born-path writer opened an already-merged receipt for write — "
        "OMN-16071 Defect 1"
    )


def test_the_omn_15785_contract_guard_is_retained(tmp_path: Path) -> None:
    """CONTROL. Contract AND receipt present is still skipped — nothing narrows."""
    _action, receipt_path = _drive_emit(tmp_path, seed_contract=True, seed_receipt=True)
    assert receipt_path.read_text(encoding="utf-8") == _MERGED_RECEIPT_BODY


def test_a_fresh_ticket_still_mints_its_slot_receipt(tmp_path: Path) -> None:
    """CONTROL. Neither file present is the born case — the mint must still happen.

    Without this the fix could pass leg 1 by never writing the slot receipt at
    all, which trades the append-only violation for a born-INELIGIBLE companion
    (``MISSING_RECEIPT``, OMN-15247 R21b).
    """
    _action, receipt_path = _drive_emit(
        tmp_path, seed_contract=False, seed_receipt=False
    )
    assert receipt_path.is_file(), "fresh ticket minted no slot receipt"
    body = receipt_path.read_text(encoding="utf-8")
    assert f"pr_number: {_PR}" in body
    assert body != _MERGED_RECEIPT_BODY
