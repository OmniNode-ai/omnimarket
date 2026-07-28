# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15323: the producer's own output must PASS ``occ-preflight / eligibility``.

This is the producer -> gate SEAM test, not two independent unit suites. It runs
the REAL gate predicate — ``omnibase_core.validation.validator_occ_merge_
eligibility.validate_occ_merge_eligibility``, the exact function
``occ-preflight.yml`` executes — over the REAL bytes
``HandlerOccObservationEffect._write_sync`` commits, against a REAL
``onex_change_control`` fixture (contract + receipts copied verbatim from OCC
``dev``; see ``tests/fixtures/occ_observation_selfbind/README.md``).

RED and GREEN come from the SAME handler run, at the two commits it makes:

  * commit A (record only) is byte-identical to what the pre-OMN-15323 producer
    emitted — the gate returns ``pr_ticket_mismatch``.
  * commit B (contract entry + PASS receipt) — the gate returns
    ``eligible=True``.

The gap this closes in ``test_bindable_pr_shape_omn_15300.py`` is precise: that
test's GREEN arm asserts ``reason not in {missing_ticket, pr_ticket_mismatch}``
and explicitly excludes contract/receipt resolution. It cannot observe the
failure that kept every observation PR unmergeable. Here the assertion is
``eligible is True``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from omnibase_core.enums.enum_occ_eligibility_reason import EnumOccEligibilityReason
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
)

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATION_EVIDENCE_TICKET,
    occ_observation_contract_relpath,
    occ_observation_evidence_item_id,
    occ_observation_receipt_relpath,
    occ_observation_record_relpath,
)
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
    render_occ_observation_pr_body,
    render_occ_observation_pr_title,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3] / "fixtures" / "occ_observation_selfbind"
)

#: The OCC PR number the observation PR would receive. Deliberately never used
#: for binding: the receipt binds through ``commit_sha``, so eligibility must
#: pass with a PR number the producer never saw.
OCC_PR_NUMBER = 5999


@pytest.fixture(autouse=True)
def _clear_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear inherited GIT_* vars (reference_git_env_vars_override_c_and_cwd)."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _record() -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1931,
        head_sha="d1da60916990aa83ac4c5ddd063a1bb0f18b79da",
        policy_version="v1",
        workflow_run_id=30376361463,
        run_attempt=1,
        recorded_at="2026-07-28T16:02:51Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1931,
            occ_pr_number=5294,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at="2026-07-28T16:02:45+00:00",
            reason="ACCEPTED: companion byte-matches the canonical plan.",
        ),
    )


def _seed_occ(tmp_path: Path) -> Path:
    """A real git repo carrying the REAL OCC contract + receipts fixture."""
    seed = tmp_path / "seed"
    shutil.copytree(FIXTURE_ROOT, seed)
    _git(seed, "init", "-q")
    _git(seed, "config", "user.name", "test")
    _git(seed, "config", "user.email", "test@omninode.ai")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "OCC fixture base")
    return seed


class _Run:
    """One full mutate-mode handler run, with both pushed trees captured."""

    def __init__(self) -> None:
        self.snapshots: list[Path] = []
        self.commit_shas: list[str] = []
        self.commit_texts: list[str] = []
        self.branch: str = ""


async def _run_producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Run:
    seed = _seed_occ(tmp_path)
    handler = HandlerOccObservationEffect()
    run = _Run()

    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers."
        "handler_occ_observation_effect._resolve_github_token",
        lambda: "dummy-token",
    )
    monkeypatch.setattr(handler, "_open_pr_for_identity", lambda *_a, **_k: None)
    monkeypatch.setattr(
        handler,
        "_clone_default",
        lambda d, _t, _r: (shutil.copytree(seed, d), "dev")[1],
    )

    def _rest_json(method: str, path: str, **_kw: object) -> dict[str, object]:
        """Stand in for the OCC remote read-back of the pushed record commit."""
        assert method == "GET", method
        assert "/commits/" in path, path
        return {"sha": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers."
        "handler_occ_observation_effect.rest_json",
        _rest_json,
    )

    def _capture_push(clone_dir: str, branch: str, _t: str, _r: str) -> None:
        run.branch = branch
        dest = tmp_path / f"pushed-{len(run.snapshots)}"
        shutil.copytree(clone_dir, dest)
        run.snapshots.append(dest)
        run.commit_shas.append(_git(Path(clone_dir), "rev-parse", "HEAD"))
        run.commit_texts.append(_git(Path(clone_dir), "log", "-1", "--format=%s"))

    monkeypatch.setattr(handler, "_push", _capture_push)
    monkeypatch.setattr(
        handler,
        "_open_or_sync_occ_pr",
        lambda *_a, **_k: (OCC_PR_NUMBER, "https://example.invalid/pr"),
    )

    result = await handler.handle(
        ModelOccObservationEffectRequest(record=_record(), mode="mutate")
    )
    assert result.mode == "mutate"
    assert result.occ_pr_number == OCC_PR_NUMBER
    return run


def _eligibility(run: _Run, *, snapshot_index: int) -> object:
    """Run the REAL gate predicate over one of the pushed trees."""
    tree = run.snapshots[snapshot_index]
    relpath = occ_observation_record_relpath(_record())
    ticket = OCC_OBSERVATION_EVIDENCE_TICKET
    snapshot = ModelOccEligibilityInput(
        repo="onex_change_control",
        pr_number=OCC_PR_NUMBER,
        pr_title=render_occ_observation_pr_title(ticket, relpath),
        pr_body=render_occ_observation_pr_body(ticket, relpath),
        pr_branch=run.branch,
        pr_commit_shas=tuple(run.commit_shas[: snapshot_index + 1]),
        pr_commit_texts=tuple(run.commit_texts[: snapshot_index + 1]),
        occ_commit_sha="0" * 40,
        contracts_dir=tree / "contracts",
        receipts_dir=tree / "drift" / "dod_receipts",
    )
    return validate_occ_merge_eligibility(snapshot)


@pytest.mark.unit
@pytest.mark.asyncio
class TestProducerOutputAgainstTheRealGate:
    async def test_red_record_only_commit_is_ineligible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-fix producer output — the record commit — still fails.

        Pins the RED arm to executed bytes rather than to a remembered
        symptom: if a future change makes the record commit self-sufficient,
        this test must be re-derived, not silently kept green.
        """
        run = await _run_producer(tmp_path, monkeypatch)
        result = _eligibility(run, snapshot_index=0)
        assert result.eligible is False
        assert result.reason is EnumOccEligibilityReason.PR_TICKET_MISMATCH
        assert OCC_OBSERVATION_EVIDENCE_TICKET in result.detail

    async def test_green_self_bind_commit_is_eligible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped producer output passes. ``eligible is True``, nothing weaker."""
        run = await _run_producer(tmp_path, monkeypatch)
        result = _eligibility(run, snapshot_index=1)
        assert result.eligible is True, result.detail
        assert result.reason is EnumOccEligibilityReason.ELIGIBLE

    async def test_binding_is_the_record_commit_not_the_pr_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GREEN must not depend on the PR number the producer never knew.

        The receipt is authored before the PR exists, so a ``pr_number``-based
        binding would be a guess. Re-running the gate with a completely
        different PR number must still pass.
        """
        run = await _run_producer(tmp_path, monkeypatch)
        tree = run.snapshots[1]
        relpath = occ_observation_record_relpath(_record())
        ticket = OCC_OBSERVATION_EVIDENCE_TICKET
        snapshot = ModelOccEligibilityInput(
            repo="onex_change_control",
            pr_number=424242,
            pr_title=render_occ_observation_pr_title(ticket, relpath),
            pr_body=render_occ_observation_pr_body(ticket, relpath),
            pr_branch=run.branch,
            pr_commit_shas=tuple(run.commit_shas),
            pr_commit_texts=tuple(run.commit_texts),
            occ_commit_sha="0" * 40,
            contracts_dir=tree / "contracts",
            receipts_dir=tree / "drift" / "dod_receipts",
        )
        result = validate_occ_merge_eligibility(snapshot)
        assert result.eligible is True, result.detail

    async def test_unbinding_the_receipt_turns_the_gate_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prove the receipt's binding — not its mere presence — carries GREEN.

        Withholding the record commit's sha from the PR's commit set leaves the
        receipt on disk and the entry declared, yet the gate must reject: this
        is the RED-against-exists-but-wrong arm.
        """
        run = await _run_producer(tmp_path, monkeypatch)
        tree = run.snapshots[1]
        relpath = occ_observation_record_relpath(_record())
        ticket = OCC_OBSERVATION_EVIDENCE_TICKET
        snapshot = ModelOccEligibilityInput(
            repo="onex_change_control",
            pr_number=OCC_PR_NUMBER,
            pr_title=render_occ_observation_pr_title(ticket, relpath),
            pr_body=render_occ_observation_pr_body(ticket, relpath),
            pr_branch=run.branch,
            pr_commit_shas=(run.commit_shas[1],),
            pr_commit_texts=tuple(run.commit_texts),
            occ_commit_sha="0" * 40,
            contracts_dir=tree / "contracts",
            receipts_dir=tree / "drift" / "dod_receipts",
        )
        result = validate_occ_merge_eligibility(snapshot)
        assert result.eligible is False
        assert result.reason is EnumOccEligibilityReason.PR_TICKET_MISMATCH


@pytest.mark.unit
@pytest.mark.asyncio
class TestWriteSurfaceStaysBounded:
    async def test_exactly_three_paths_are_touched_and_none_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The widened allowlist is still an exact set derived from this run.

        ``grants/**`` and ``allowlists/**`` are the paths the OCC write token
        must never reach (OMN-14919); the assertion is the stronger one — the
        diff is EXACTLY the three expected paths, all adds/modifies.
        """
        run = await _run_producer(tmp_path, monkeypatch)
        tree = run.snapshots[1]
        diff = _git(tree, "diff", "--name-status", run.commit_shas[0] + "~1", "HEAD")
        entries = {
            line.split("\t")[-1]: line.split("\t")[0] for line in diff.splitlines()
        }
        ticket = OCC_OBSERVATION_EVIDENCE_TICKET
        item = occ_observation_evidence_item_id(_record())
        assert set(entries) == {
            occ_observation_record_relpath(_record()),
            occ_observation_contract_relpath(ticket),
            occ_observation_receipt_relpath(ticket, item),
        }, entries
        assert not any(status.startswith("D") for status in entries.values()), entries

    async def test_contract_append_leaves_prior_entries_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Append-only in the byte sense, which is what OCC's gate enforces.

        Every pre-existing line of the contract survives unchanged, so no other
        entry's per-entry hash — and therefore no other merged receipt — can be
        invalidated by this producer.
        """
        run = await _run_producer(tmp_path, monkeypatch)
        contract_relpath = occ_observation_contract_relpath(
            OCC_OBSERVATION_EVIDENCE_TICKET
        )
        before = (FIXTURE_ROOT / contract_relpath).read_text(encoding="utf-8")
        after = (run.snapshots[1] / contract_relpath).read_text(encoding="utf-8")
        assert after.startswith(before), "existing contract bytes were rewritten"
        assert after != before, "no dod_evidence entry was appended"
