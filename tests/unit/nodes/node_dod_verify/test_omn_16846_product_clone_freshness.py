# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16846: the tree a behaviour check runs in is part of the verdict.

Two independent unsoundnesses in the LOCAL ``dod_verify`` path, both
reproduced by execution 2026-08-28 and both pinned here.

**D2 — no freshness assertion on the product clone.** ``node_dod_verify``
refreshes the CONTRACT repo and stamps ``occ_refresh_outcome`` /
``occ_resolved_sha`` on the receipt. It asserted nothing whatsoever about the
clone the ``test_passes`` commands actually execute in, and recorded no tree
SHA for it. Verifying OMN-16037, the canonical ``omnibase_core`` clone sat 2
commits behind ``origin/dev`` and did not contain the merge under adjudication
(``d89b1076``, #1604); ``uv run pytest tests/unit/cli/test_cli_user_config.py``
returned ``collected 0 items / no tests ran`` — indistinguishable from "the
tests were never written". After ``git pull --ff-only`` the identical command
returned ``33 passed``. 9 of 12 canonical clones were behind that same session
(up to -22 commits), so a stale product clone is the machine's normal state.

**D1/AC3 — a venv-purity refusal recorded as a substantive failure.** The
OMN-15620 gate fires in ``pytest_configure``, before collection and before any
test module import. The command exits non-zero having made no statement about
the product, yet run 33194402437 recorded all three OMN-16759 behaviour checks
as ``FAILED ... Canonical venv is IMPURE`` with ``behavior_proving=0`` — a
false FAILED naming a venv, indistinguishable in the receipt from a ticket
that genuinely is not proven.

Both are recorded SKIPPED with a NAMED cause, never FAILED and never verified:
the OMN-16788 shape, where the block survives intact and only its recording
becomes honest. ``HandlerDodVerify`` already refuses VERIFIED while any
``unverifiable_cause`` is present, which is asserted here too rather than
assumed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    EnumProductCloneFreshness,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

pytestmark = pytest.mark.unit

# The verbatim banner tests/conftest.py prints via pytest.exit() when the
# OMN-15620 gate refuses the venv. Captured from omnibase_infra's conftest
# (`pytest.exit(f"OMN-15620 venv-purity gate: {exc}", returncode=1)`) plus
# venv_purity.py's message body.
_PURITY_BANNER = (
    "OMN-15620 venv-purity gate: Canonical venv is IMPURE: 1 distribution(s) "
    "provide 'onex.nodes' entry points but are not declared in uv.lock: "
    "omnimarket==0.4.11"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _make_origin_and_clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a real origin plus a CURRENT clone and a clone one commit BEHIND.

    Deliberately real git repositories, not a fixture that stubs the git
    calls: AC1/AC4 say a hand-built fixture that never exercises the real
    mechanism does not discharge the requirement, and the failure being
    reproduced is entirely about what ``git`` reports for a real tree.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=dev", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=dev", ".")
    _git(seed, "config", "user.email", "t@t.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "marker.txt").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "first")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "dev")

    # The clone that will be left BEHIND: cloned at v1, before the second
    # commit exists at all.
    stale = tmp_path / "stale"
    _git(tmp_path, "clone", str(origin), str(stale))

    # The commit under adjudication — the one a stale clone cannot see.
    (seed / "proof.txt").write_text("the work under adjudication\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "second")
    _git(seed, "push", "origin", "dev")

    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", str(origin), str(fresh))
    return origin, stale, fresh


def _item(cwd: Path, command: str) -> dict[str, object]:
    return {
        "id": "dod-16846-behaviour",
        "description": "behaviour check bound to a product clone",
        "checks": [
            {
                "check_type": "test_passes",
                "command": command,
                "cwd": str(cwd),
            }
        ],
    }


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    # _resolve_cwd containment-checks every declared cwd against OMNI_HOME.
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", raising=False)
    # These items bind to no PR; leaving the OMN-14207 live check on would
    # shell out to `gh` for a merge state that is irrelevant here.
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    return EvidenceCollector()


# ---------------------------------------------------------------------------
# D2 / AC4 — the same check, the same command, two trees, two verdicts
# ---------------------------------------------------------------------------


def test_a_stale_clone_and_a_current_clone_disagree_on_the_same_command(
    tmp_path: Path,
) -> None:
    """RED-first premise: this is the defect, stated without the fix.

    Without a freshness assertion the runner's verdict is decided by which
    tree the canonical clone happens to sit at — exactly the OMN-16037
    near-miss. Asserted directly against git so the rest of the file is
    testing a real divergence rather than a supposed one.
    """
    _origin, stale, fresh = _make_origin_and_clones(tmp_path)
    probe = ["test", "-f", "proof.txt"]

    assert subprocess.run(probe, cwd=stale).returncode != 0
    assert subprocess.run(probe, cwd=fresh).returncode == 0


def test_stale_product_clone_is_skipped_with_a_named_cause_not_failed(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    _origin, stale, _fresh = _make_origin_and_clones(tmp_path)

    result = collector._check_evidence_item(_item(stale, "test -f proof.txt"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE
    )
    assert result.message is not None
    assert "PRODUCT_CLONE_NOT_FRESH" in result.message


def test_the_stale_command_is_never_executed(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """Fail closed means NOT RUN, not run-and-discard.

    Proven by a command with a side effect: if the file appears, the runner
    executed against the stale tree despite refusing it.
    """
    _origin, stale, _fresh = _make_origin_and_clones(tmp_path)
    canary = stale / "executed.canary"

    result = collector._check_evidence_item(_item(stale, f"touch {canary}"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert not canary.exists()


def test_a_current_clone_verifies_and_records_the_tree_it_ran_in(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """AC5: the receipt names WHICH tree produced the verdict."""
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    head = _git(fresh, "rev-parse", "HEAD")

    result = collector._check_evidence_item(_item(fresh, "test -f proof.txt"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.unverifiable_cause is None
    assert len(result.product_clones) == 1
    clone = result.product_clones[0]
    assert clone.freshness is EnumProductCloneFreshness.FRESH
    assert clone.head_sha == head
    assert clone.upstream_ref == "origin/dev"
    assert clone.behind_count == 0


def test_the_stale_result_also_records_the_tree_it_refused(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    _origin, stale, _fresh = _make_origin_and_clones(tmp_path)
    head = _git(stale, "rev-parse", "HEAD")

    result = collector._check_evidence_item(_item(stale, "true"), "OMN-1")

    assert len(result.product_clones) == 1
    clone = result.product_clones[0]
    assert clone.freshness is EnumProductCloneFreshness.STALE
    assert clone.head_sha == head
    assert clone.behind_count == 1


def test_a_clone_ahead_of_its_upstream_is_fresh_not_stale(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """A worktree parked on the branch under review is the legitimate shape.

    Only MISSING commits falsify a verdict. Refusing an ahead-of-upstream tree
    would make the gate refuse to verify the very branch it was pointed at.
    """
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    _git(fresh, "config", "user.email", "t@t.invalid")
    _git(fresh, "config", "user.name", "t")
    (fresh / "local.txt").write_text("unpushed\n")
    _git(fresh, "add", "-A")
    _git(fresh, "commit", "-m", "ahead")

    result = collector._check_evidence_item(_item(fresh, "test -f proof.txt"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.product_clones[0].freshness is EnumProductCloneFreshness.FRESH


def test_a_modified_tracked_file_is_dirty_and_blocks(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    (fresh / "marker.txt").write_text("locally modified\n")

    result = collector._check_evidence_item(_item(fresh, "true"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_FRESHNESS_UNKNOWN
    )
    assert result.product_clones[0].freshness is EnumProductCloneFreshness.DIRTY


def test_untracked_files_are_not_dirt(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """Every canonical clone carries build artefacts and caches.

    Treating those as dirt would make the gate unusable without catching the
    misattribution it exists for — an untracked file changes no tracked test.
    """
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    (fresh / "build.log").write_text("artefact\n")

    result = collector._check_evidence_item(_item(fresh, "true"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED


def test_a_cwd_that_is_not_a_repository_is_out_of_scope_not_refused(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """A scratch directory has no tree to be stale against.

    Deliberately NOT_APPLICABLE rather than UNKNOWN: UNKNOWN means "there is a
    tree here and we could not pin it", which is a refusal, and refusing every
    generated working directory would break checks that have nothing to do
    with a clone. ``_resolve_cwd`` has already proven the path exists and is
    contained inside ``OMNI_HOME``, so this cannot silently absorb a typo'd
    repository path — that fails earlier, with its own message.
    """
    plain = tmp_path / "not_a_repo"
    plain.mkdir()

    result = collector._check_evidence_item(_item(plain, "true"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.unverifiable_cause is None
    assert result.product_clones == ()


def test_a_clone_with_no_upstream_fails_closed_as_unknown(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    _git(fresh, "checkout", "-b", "detached-work")

    result = collector._check_evidence_item(_item(fresh, "true"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause
        is EnumEvidenceUnverifiableCause.PRODUCT_CLONE_FRESHNESS_UNKNOWN
    )


def test_the_named_override_executes_and_stays_recorded(
    collector: EvidenceCollector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sole escape hatch is named and logged, never silent.

    It restores execution; it does not erase the provenance that says which
    tree ran.
    """
    _origin, stale, _fresh = _make_origin_and_clones(tmp_path)
    monkeypatch.setenv("DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE", "1")

    result = collector._check_evidence_item(_item(stale, "true"), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.product_clones[0].freshness is EnumProductCloneFreshness.STALE


def test_a_check_with_no_declared_cwd_is_untouched(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """The gate is scoped to what AC5 names: repositories a check's cwd names.

    Checks that inherit the caller's directory (or the auto-injected OCC root,
    whose freshness the OCC provenance fields already pin) keep their existing
    behaviour exactly, and record no product-clone provenance.
    """
    item = {
        "id": "dod-no-cwd",
        "description": "inherits the caller's directory",
        "checks": [{"check_type": "command", "command": "true"}],
    }

    result = collector._check_evidence_item(item, "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert result.product_clones == ()


def test_the_freshness_probe_fetches_once_per_repository(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """Memoised per repo: a contract with a dozen checks must not fetch twelve times."""
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    fetches: list[tuple[str, ...]] = []
    real_run_git = collector._run_git

    def _recording(repo: Path, *args: str) -> tuple[int, str, str]:
        if args and args[0] == "fetch":
            fetches.append(args)
        return real_run_git(repo, *args)

    collector._run_git = _recording  # type: ignore[method-assign]

    item = {
        "id": "dod-many",
        "description": "several checks, one repository",
        "checks": [
            {"check_type": "test_passes", "command": "true", "cwd": str(fresh)}
            for _ in range(4)
        ],
    }
    result = collector._check_evidence_item(item, "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.VERIFIED
    assert len(fetches) == 1
    assert len(result.product_clones) == 1


# ---------------------------------------------------------------------------
# D1 / AC3 — a purity refusal is not a substantive failure
# ---------------------------------------------------------------------------


def test_a_venv_purity_refusal_is_skipped_with_a_named_cause(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)
    # Reproduce the runner's observed shape: the gate's banner on stdout and a
    # non-zero exit, with nothing collected.
    command = f"echo {_PURITY_BANNER!r}; exit 1"

    result = collector._check_evidence_item(_item(fresh, command), "OMN-1")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert result.unverifiable_cause is EnumEvidenceUnverifiableCause.GATE_VENV_IMPURE
    assert result.message is not None
    assert "GATE_VENV_IMPURE" in result.message


def test_an_ordinary_test_failure_is_still_a_substantive_failure(
    collector: EvidenceCollector, tmp_path: Path
) -> None:
    """The carve-out is the gate's own banner, not any non-zero exit.

    A failure that merely mentions the ticket number, or names the gate
    without being its refusal, must not be launderable into a skip — that
    would turn OMN-16788's honest-recording change into a fail-open.
    """
    _origin, _stale, fresh = _make_origin_and_clones(tmp_path)

    for command in (
        "echo '1 failed, 0 passed'; exit 1",
        "echo 'OMN-15620 venv-purity gate: all clear'; exit 1",
        "echo 'Canonical venv is IMPURE'; exit 1",
    ):
        result = collector._check_evidence_item(_item(fresh, command), "OMN-1")
        assert result.status is EnumEvidenceCheckStatus.FAILED, command
        assert result.unverifiable_cause is None, command


# ---------------------------------------------------------------------------
# Verdict-level: neither cause can release a Done-flip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cause",
    [
        EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE,
        EnumEvidenceUnverifiableCause.PRODUCT_CLONE_FRESHNESS_UNKNOWN,
        EnumEvidenceUnverifiableCause.GATE_VENV_IMPURE,
    ],
)
def test_neither_new_cause_can_reach_a_verified_verdict(
    cause: EnumEvidenceUnverifiableCause,
) -> None:
    """Blocking is preserved; only the recording changed.

    A sibling check VERIFIES in the same run, so a plain (deliberate) SKIPPED
    would have let the ticket reach VERIFIED. It must not.
    """
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(correlation_id=uuid4(), ticket_id="OMN-16846"),
        evidence_results=[
            ModelEvidenceCheckResult(
                evidence_id="dod-ok",
                description="a sibling that really did pass",
                status=EnumEvidenceCheckStatus.VERIFIED,
            ),
            ModelEvidenceCheckResult(
                evidence_id="dod-blocked",
                description="never executed",
                status=EnumEvidenceCheckStatus.SKIPPED,
                unverifiable_cause=cause,
            ),
        ],
    )

    assert isinstance(state, ModelDodVerifyState)
    assert state.status is not EnumDodVerifyStatus.VERIFIED
    assert state.error_message is not None
    assert "EVIDENCE_UNVERIFIABLE" in state.error_message
    assert cause.value in state.error_message
