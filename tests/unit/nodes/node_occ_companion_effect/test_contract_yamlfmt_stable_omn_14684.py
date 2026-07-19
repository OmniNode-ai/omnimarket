# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14684 (parent OMN-14643 / WS5): yamlfmt-before-hash guard for the write-EFFECT.

Every downstream / self-bind / supersede receipt binds ``contract_sha256 =
sha256(contract-file-bytes)``. ``onex_change_control``'s hosted pre-commit runs
``yamlfmt`` on merge; if a pushed ``contracts/<ticket>.yaml`` is not ALREADY a
yamlfmt no-op, hosted CI reformats it, the committed bytes change, and every
stamped ``contract_sha256`` goes stale — the recompute + rebind cascade that
recurred on essentially every OCC companion PR in the 2026-07-10 → 2026-07-16
ledger (OMN-14285 / OMN-14326 / OMN-14655 …).

``HandlerOccCompanionEffect._assert_contracts_yamlfmt_stable`` fails CLOSED
BEFORE the push if a written contract is not yamlfmt-idempotent, so the stamped
hash is trusted only over yamlfmt-stable bytes. The guard is ASSERT-ONLY (the
EFFECT never rewrites compute bytes — the RSD-5 attestation oracle byte-diffs the
pure COMPUTE output).

Driven against real files on disk with the OCC repo's own ``.yamlfmt`` config, so
the RED case proves the guard actually fires (not vacuously green;
feedback_prove_red_against_exists_but_wrong).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from omnimarket.events.occ_companion import (
    EnumCompanionFileKind,
    ModelCompanionFile,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)

pytestmark = pytest.mark.unit

_YAMLFMT = shutil.which("yamlfmt")
_OCC_YAMLFMT_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "occ_companion_golden"
    / "occ.yamlfmt"
)
_requires_yamlfmt = pytest.mark.skipif(
    _YAMLFMT is None, reason="yamlfmt binary not available"
)


# ---------------------------------------------------------------------------
# Request builders (mirror the OMN-14710 golden suite)
# ---------------------------------------------------------------------------
def _probe(
    number: int = 321, repo: str = "OmniNode-ai/omnimarket"
) -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {number} --repo {repo} --json number,state",
        stdout=f'{{"number":{number},"state":"OPEN"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


# A pass-2 request whose OCC PR is known → the contract also declares the
# self-bind item and the plan emits every fresh-path companion file kind.
_PASS2: dict[str, object] = {
    "occ_pr_number": 4284,
    "occ_head_sha": "c" * 40,
    "occ_repo": "OmniNode-ai/onex_change_control",
    "occ_probe": ModelObservedProbe(
        command="gh pr view 4284 --repo OmniNode-ai/onex_change_control --json number,state",
        stdout='{"number":4284,"state":"OPEN"}',
        exit_code=0,
    ),
}


def _contract_file(files: tuple[ModelCompanionFile, ...]) -> ModelCompanionFile:
    return next(f for f in files if f.kind == EnumCompanionFileKind.CONTRACT)


def _write_clone(tmp_path: Path, files: tuple[ModelCompanionFile, ...]) -> str:
    """Materialize a plan's files + the OCC ``.yamlfmt`` into a fake clone dir."""
    (tmp_path / ".yamlfmt").write_text(
        _OCC_YAMLFMT_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for f in files:
        path = tmp_path / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")
    return str(tmp_path)


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _yamlfmt_format(content: str) -> str:
    """Return the yamlfmt-canonical bytes for ``content`` under the OCC config."""
    assert _YAMLFMT is not None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        name = tf.name
    try:
        subprocess.run(
            [_YAMLFMT, "-conf", str(_OCC_YAMLFMT_FIXTURE), name],
            capture_output=True,
            text=True,
            check=False,
        )
        return Path(name).read_text(encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)


class TestContractYamlfmtStableGuard:
    @_requires_yamlfmt
    def test_fresh_path_contract_passes_guard(self, tmp_path: Path) -> None:
        # The producer's real fresh-path contract is yamlfmt-idempotent, so the
        # guard is a no-op — no raise.
        plan = compute_companion_plan(_request(**_PASS2))
        clone = _write_clone(tmp_path, plan.companion_files)
        HandlerOccCompanionEffect()._assert_contracts_yamlfmt_stable(
            clone, plan.companion_files
        )

    @_requires_yamlfmt
    def test_stamped_contract_sha256_matches_post_yamlfmt_bytes(self) -> None:
        # Acceptance criterion #2: the producer's stamped contract_sha256 equals
        # the hash of the POST-yamlfmt contract bytes — the whole point of the
        # ordering fix. Proven end-to-end: the contract is a yamlfmt no-op AND
        # every receipt that cites the hash cites sha256(post-yamlfmt bytes).
        plan = compute_companion_plan(_request(**_PASS2))
        contract = _contract_file(plan.companion_files)
        post_fmt_hash = _sha256(_yamlfmt_format(contract.content))
        assert _sha256(contract.content) == post_fmt_hash, (
            "producer contract is not yamlfmt-idempotent — stamped hash would go "
            "stale after hosted CI reformats it"
        )
        receipts = [
            f for f in plan.companion_files if f.kind != EnumCompanionFileKind.CONTRACT
        ]
        assert receipts, "expected receipts binding the contract hash"
        for r in receipts:
            assert f"sha256:{post_fmt_hash}" in r.content, (
                f"{r.path} does not bind sha256(post-yamlfmt contract bytes)"
            )

    @_requires_yamlfmt
    def test_non_yamlfmt_clean_contract_is_rejected(self, tmp_path: Path) -> None:
        # RED control: a contract that yamlfmt would reformat (bad indentation +
        # trailing whitespace + a construct from the OMN-14453 corruption note)
        # must trip the guard — proving the assertion is load-bearing.
        dirty = ModelCompanionFile(
            path="contracts/OMN-9999.yaml",
            content=(
                "---\n"
                "ticket_id:    OMN-9999   \n"
                "dod_evidence:\n"
                "- id: dod-1\n"
                "-   id: dod-2\n"
            ),
            kind=EnumCompanionFileKind.CONTRACT,
            ticket_id="OMN-9999",
        )
        clone = _write_clone(tmp_path, (dirty,))
        with pytest.raises(RuntimeError, match="yamlfmt-stable"):
            HandlerOccCompanionEffect()._assert_contracts_yamlfmt_stable(
                clone, (dirty,)
            )

    @_requires_yamlfmt
    def test_guard_is_contract_scoped_and_ignores_dirty_receipts(
        self, tmp_path: Path
    ) -> None:
        # The guard protects contract_sha256, which binds the CONTRACT file only.
        # A yamlfmt-dirty SUPERSEDE receipt (the OMN-14714 merged-path class) does
        # NOT invalidate the contract hash, so the guard must not fire on it — that
        # dirtiness is tracked separately, not this guard's concern.
        dirty_receipt = ModelCompanionFile(
            path="drift/dod_receipts/OMN-9999/dod-x/command.supersede.321.yaml",
            content="---\nstatus:    PASS   \nnested:\n-   a\n",
            kind=EnumCompanionFileKind.SUPERSEDE_RECEIPT,
            ticket_id="OMN-9999",
            contract_sha256="sha256:" + "a" * 64,
        )
        clone = _write_clone(tmp_path, (dirty_receipt,))
        # No CONTRACT-kind file present → guard returns without inspecting anything.
        HandlerOccCompanionEffect()._assert_contracts_yamlfmt_stable(
            clone, (dirty_receipt,)
        )

    def test_missing_yamlfmt_binary_skips_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When yamlfmt is unavailable the guard skips (loud warning) rather than
        # passing silently OR blocking a live author — hosted CI is the backstop.
        monkeypatch.setattr(
            "omnimarket.nodes.node_occ_companion_effect.handlers."
            "handler_occ_companion_effect.shutil.which",
            lambda _name: None,
        )
        dirty = ModelCompanionFile(
            path="contracts/OMN-9999.yaml",
            content="---\nticket_id:    OMN-9999   \n",
            kind=EnumCompanionFileKind.CONTRACT,
            ticket_id="OMN-9999",
        )
        clone = _write_clone(tmp_path, (dirty,))
        # Even a would-be-dirty contract does not raise when yamlfmt is absent.
        HandlerOccCompanionEffect()._assert_contracts_yamlfmt_stable(clone, (dirty,))
