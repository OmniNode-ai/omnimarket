# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16859 AC3a — the born ``test_passes`` receipt tells the truth.

The defect this closes
----------------------
The born path minted the diff-derived behavior receipt with ``status: PASS``
behind a ``gh pr view <n> --json number,state,headRefName`` probe. The declared
check is a pytest run in the product repo; the probe is a PR-existence read.
Every PR on GitHub that exists satisfies it. So the receipt asserted an outcome
of a run that never happened — the exact false-evidence class the Receipt
Honesty Gate exists to catch, and the reason four separate lanes hand-authored
a replacement receipt on 2026-08-28 alone.

``EnumReceiptStatus.PENDING`` already carries precisely the right meaning —
"the probe was allocated but has not yet executed" — and ``ModelDodReceipt``
Rule 3 already exempts PENDING from the non-empty-``probe_stdout`` requirement.
So an honest born receipt needs no schema change; it needs the producer to stop
claiming PASS.

Why this is gated per repo, and why that gate is fail-SAFE
---------------------------------------------------------
A PENDING receipt is non-PASS, so it holds the companion ineligible until
something executes the check. That is correct and deliberate — but only where
something *will*. The emitter serves EVERY product repo from the .201 effects
runtime, while the AC3b runner is piloted on omnimarket alone. Minting PENDING
everywhere would jam every repo that has no runner, trading a dishonest PASS
for a universal deadlock.

So the honest status is minted only for a repo the runner covers, and the
default for every other repo is unchanged, byte-for-byte. The gate is the
rollout knob: a repo joins by installing the runner workflow and being added to
``RUNNER_COVERED_REPOS`` — never the other way round. Tests below pin BOTH
directions, because a gate that silently defaulted to PENDING would be the
outage, and a gate that silently defaulted to PASS forever would be the
dishonesty.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    BEHAVIOR_PROOF_EVIDENCE_ID,
    RUNNER_COVERED_REPOS,
    born_slot_receipt_status,
    render_downstream_receipt,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

_TICKET = "OMN-16859"
_REPO = "OmniNode-ai/omnimarket"
_PR = 322
_SRC_FILE = "src/omnimarket/nodes/node_emit_daemon/handlers/handler_emit_daemon.py"
_TEST_FILE = "tests/unit/nodes/node_emit_daemon/test_fanout_partial_drop_omn16599.py"


class _FakeTempDir:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same pin the OMN-16892 module uses; this module's properties are
    binding-orthogonal and the content-bound default aborts the mint."""
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


def _pr_data() -> dict[str, object]:
    return {
        "body": f"Closes {_TICKET}",
        "title": f"fix({_TICKET}): born receipt records PENDING, not a fabricated PASS",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _drive_emit(
    tmp_path: Path, *, changed_files: tuple[str, ...], repo: str = _REPO
) -> Path:
    """Run the REAL ``_emit_companion_sync`` against a temp clone.

    Copied in shape from the OMN-16892 module deliberately: the file writes,
    the contract rendering and the append-only guard all run for real, so what
    this asserts is what the live producer pushes.
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
        emitter._emit_companion_sync(repo, _PR, None)
    return clone_root


def _born_receipt(clone_root: Path, check_type: str) -> ModelDodReceipt:
    path = (
        clone_root
        / "drift"
        / "dod_receipts"
        / _TICKET
        / BEHAVIOR_PROOF_EVIDENCE_ID
        / f"{check_type}.yaml"
    )
    assert path.is_file(), f"producer minted no receipt at {path}"
    return ModelDodReceipt.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


# ---------------------------------------------------------------------------
# The live producer
# ---------------------------------------------------------------------------


class TestTheBornBehaviorReceiptIsHonest:
    def test_the_born_test_passes_receipt_is_pending_not_pass(
        self, tmp_path: Path
    ) -> None:
        """The load-bearing assertion of AC3a.

        PASS here is a claim about a pytest run this producer structurally
        cannot perform: it runs in the effects runtime, against the GitHub API,
        with no checkout of the product repo.
        """
        clone_root = _drive_emit(tmp_path, changed_files=(_SRC_FILE, _TEST_FILE))
        receipt = _born_receipt(clone_root, "test_passes")

        assert receipt.status is EnumReceiptStatus.PENDING, (
            "the born behavior receipt still claims a status for a check "
            "nothing executed"
        )

    def test_the_pending_receipt_names_the_surface_that_owes_the_run(
        self, tmp_path: Path
    ) -> None:
        """A PENDING receipt with no addressee is a mystery, not a record.

        Four lanes re-diagnosed this defect from scratch because the failure
        text named neither the producer nor the surface that would fix it.
        """
        clone_root = _drive_emit(tmp_path, changed_files=(_SRC_FILE, _TEST_FILE))
        receipt = _born_receipt(clone_root, "test_passes")

        assert receipt.actual_output is not None
        assert "PENDING" in receipt.actual_output
        assert "occ-receipt-runner" in receipt.actual_output

    def test_the_probe_is_not_dressed_up_as_the_declared_check(
        self, tmp_path: Path
    ) -> None:
        """The receipt must not imply the PR read proved the pytest run."""
        clone_root = _drive_emit(tmp_path, changed_files=(_SRC_FILE, _TEST_FILE))
        receipt = _born_receipt(clone_root, "test_passes")

        assert receipt.check_value.startswith("uv run pytest")
        assert "gh pr view" in receipt.probe_command
        assert receipt.check_value != receipt.probe_command

    def test_the_owed_branch_admissibility_receipt_still_passes(
        self, tmp_path: Path
    ) -> None:
        """Blast-radius containment.

        When the diff carries no pytest target the producer mints the
        ``command`` admissibility item instead — an OCC-runner-executed check
        this producer's probe genuinely evidences. Its PASS is untouched. If
        this regressed, every diff without a test file would jam.
        """
        clone_root = _drive_emit(tmp_path, changed_files=(_SRC_FILE,))
        path = clone_root / "drift" / "dod_receipts" / _TICKET
        minted = sorted(p for p in path.rglob("*.yaml"))
        assert minted, "producer minted no receipt on the OWED branch"
        for receipt_path in minted:
            receipt = ModelDodReceipt.model_validate(
                yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
            )
            assert receipt.status is EnumReceiptStatus.PASS


# ---------------------------------------------------------------------------
# The rollout gate, in both directions
# ---------------------------------------------------------------------------


class TestTheRunnerCoverageGate:
    def test_a_runner_covered_repo_gets_the_honest_pending(self) -> None:
        assert (
            born_slot_receipt_status(repo=_REPO, check_type="test_passes")
            == EnumReceiptStatus.PENDING.value
        )

    def test_a_repo_without_a_runner_is_unchanged(self) -> None:
        """Fail-SAFE, and the reason is the whole design.

        A PENDING receipt is non-PASS. In a repo where nothing will ever
        execute the check, minting PENDING converts a dishonest-but-moving
        pipeline into a permanently stuck one — on ~55% of PRs. The dishonesty
        is fixed by giving the repo a runner, not by jamming it first.
        """
        assert (
            born_slot_receipt_status(
                repo="OmniNode-ai/omnibase_infra", check_type="test_passes"
            )
            == EnumReceiptStatus.PASS.value
        )

    def test_a_non_executable_check_type_is_never_downgraded(self) -> None:
        """``command`` items are OCC-runner-executed; their PASS is earned."""
        assert (
            born_slot_receipt_status(repo=_REPO, check_type="command")
            == EnumReceiptStatus.PASS.value
        )

    def test_the_pilot_repo_is_the_coverage_set(self) -> None:
        """Pins the rollout knob so widening it is a deliberate, reviewed edit.

        A repo may only be added here TOGETHER with its runner workflow; the
        ordering is what keeps the gate fail-safe.
        """
        assert _REPO in RUNNER_COVERED_REPOS


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


class TestTheRendererCarriesTheStatus:
    def _render(self, **overrides: object) -> str:
        kwargs: dict[str, object] = {
            "ticket_id": _TICKET,
            "evidence_id": BEHAVIOR_PROOF_EVIDENCE_ID,
            "pr_number": _PR,
            "repo": _REPO,
            "run_timestamp": "2026-08-29T12:00:00Z",
            "commit_sha": "b" * 40,
            "branch": "jonah/omn-16859-occ-receipt-runner",
            "probe_command": f"gh pr view {_PR} --repo {_REPO} --json number,state",
            "probe_stdout": '{"number": 322, "state": "open"}',
            "exit_code": 0,
        }
        kwargs.update(overrides)
        return render_downstream_receipt(**kwargs)  # type: ignore[arg-type]

    def test_the_default_render_is_unchanged(self) -> None:
        """Every pre-OMN-16859 caller must render byte-identically.

        The renderer is shared by the CI-item, downstream and self-bind mints.
        A changed default would silently rewrite receipts this ticket has no
        business touching.
        """
        assert "status: PASS\n" in self._render()

    def test_an_explicit_pending_is_rendered(self) -> None:
        assert "status: PENDING\n" in self._render(status="PENDING")

    def test_the_rendered_pending_receipt_survives_the_model(self) -> None:
        """PENDING with empty stdout is valid ONLY because Rule 3 exempts it.

        If that exemption did not hold, the honest born receipt would be
        unparseable and the bridge would be unshippable — which is exactly the
        thing worth proving against the real model rather than assuming.

        The renderer emits ``sha256:PENDING`` placeholders that the emitter's
        rebind pass fills in once the contract is final, so the placeholders
        are substituted here exactly as that pass substitutes them. Everything
        else is the renderer's own bytes.
        """
        rendered = self._render(status="PENDING", probe_stdout="").replace(
            "sha256:PENDING", f"sha256:{'a' * 64}"
        )
        receipt = ModelDodReceipt.model_validate(yaml.safe_load(rendered))
        assert receipt.status is EnumReceiptStatus.PENDING
        assert receipt.probe_stdout.strip() == ""
