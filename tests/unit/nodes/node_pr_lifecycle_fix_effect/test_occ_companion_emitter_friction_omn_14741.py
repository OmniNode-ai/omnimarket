# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emitter golden gate for the LIVE OCC companion producer (OMN-14741).

These tests drive the REAL :class:`OccCompanionEmitter` — the artifact that
actually mints companions on the merge-sweep path — NOT the unwired
``node_occ_companion_compute`` oracle that OMN-14679 (#1789) and OMN-14710 (#1799)
hardened instead. Each test asserts a specific overnight merge-sweep friction is
closed on the live mint path, and each FAILS against the pre-fix emitter (proven
RED-vs-EXISTS-but-WRONG by stashing the src change), so a regression that
reintroduces the broken shape fails this gate before a companion can ship:

  * F-01 append-only — a prior MERGED receipt for the same ticket is never mutated
    by a later PR's emit (scoped rebind), and the append-only guard fails closed on
    any out-of-set change.
  * F-02 placeholder — every contract check_value renders in ${PR_NUMBER}/${REPO}
    form; no hardcoded integer PR number survives (clears lint-contract-check-values).
  * F-03 yamlfmt — every generated YAML file is yamlfmt-idempotent (clears the
    hosted yamlfmt Pre-commit).
  * F-04 pre-existing contract — a contract missing THIS PR's base rows gets them
    appended, so the receipts bind (no PENDING per-entry hash) and stay eligible.
  * F-17 suppression — a closed / draft / do-not-merge product PR gets NO companion.

Wired as the required ``occ-emitter-golden`` CI gate + pre-commit hook.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from omnibase_core.validation.validator_occ_merge_eligibility import (
    EnumOccEligibilityReason,
    ModelOccEligibilityInput,
    validate_occ_merge_eligibility,
)
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

# Mirror of onex_change_control/scripts/lint_contract_check_values.py's
# _HARDCODED_PR_NUMBER_RE — the exact regex the hosted lint-contract-check-values
# gate rejects (OMN-9350 / OMN-14673). Re-derived here because OCC is not an
# omnimarket dependency; the RED->GREEN proof against the REAL gate is run against
# the emitted contract and cited in the PR body.
_HARDCODED_PR_NUMBER_RE = re.compile(r"gh pr (?:checks|view|diff)\s+\d+\s")

# Mirror of onex_change_control/.yamlfmt (google/yamlfmt v0.21.0 config). Inlined
# so the gate is deterministic without a sibling-repo checkout; kept in sync with
# OCC's config (ecosystem-aligned, OMN-4862).
_OCC_YAMLFMT_CONF = (
    "formatter:\n"
    "  retain_line_breaks: true\n"
    "  max_line_length: 100\n"
    "  indent: 2\n"
    "  include_document_start: true\n"
    "  pad_line_comments: 2\n"
)


class _FakeTempDir:
    """A ``tempfile.TemporaryDirectory`` stand-in yielding a fixed path.

    Does NOT delete on exit, so the emitted companion files remain on disk for the
    test to inspect.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


def _default_pr_data() -> dict[str, object]:
    return {
        "body": "Implements the thing.",
        "title": "feat(OMN-9999): the thing",
        "head": {"sha": "b" * 40, "ref": "feature-branch"},
        "state": "open",
        "draft": False,
        "labels": [],
    }


def _run_emit(
    emitter: OccCompanionEmitter,
    tmp_path: Path,
    *,
    pr_data: dict[str, object] | None = None,
    preseed: object = None,
) -> tuple[str, Path, list[list[str]]]:
    """Drive the REAL ``_emit_companion_sync`` with a temp clone + mocked I/O.

    git + the OCC-PR-open + product-PR-patch + probe are mocked; the contract +
    receipt rendering, structural appends, file writes, and contract_sha256 rebind
    run for real so the emitted byte-shape is exercised end to end. ``preseed`` is
    a callback ``(clone_dir) -> None`` invoked after the (mocked) clone mkdir so a
    test can plant a pre-existing contract or a prior merged receipt.
    """
    clone_root = tmp_path / "onex_change_control"
    git_calls: list[list[str]] = []
    resolved_pr_data = pr_data if pr_data is not None else _default_pr_data()

    def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
        if path.endswith("/pulls/321"):  # product PR GET
            return dict(resolved_pr_data)
        if "/pulls/55" in path:  # OCC PR GET after open-or-sync
            return {"number": 55, "state": "open"}
        return {}

    def fake_run_git(argv: list[str], *, cwd: str) -> str:
        git_calls.append(argv)
        return "c" * 40 if "rev-parse" in argv else ""

    def fake_clone(cd: Path, *_a: object) -> str:
        cd.mkdir(parents=True, exist_ok=True)
        if preseed is not None:
            preseed(cd)
        return "0" * 40  # base SHA

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        patch.object(emitter, "_open_or_sync_occ_pr", return_value=55),
        patch.object(emitter, "_observe_pr_probe", return_value=("{}", 0)),
        patch.object(emitter, "_patch_evidence_source"),
        patch(
            f"{_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ):
        action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)
    return action, clone_root, git_calls


def _contract_check_values(contract_path: Path) -> list[str]:
    data = yaml.safe_load(contract_path.read_text())
    values: list[str] = []
    for item in data.get("dod_evidence") or []:
        for check in item.get("checks") or []:
            cv = check.get("check_value")
            if isinstance(cv, str):
                values.append(cv)
    return values


# ---------------------------------------------------------------------------
# F-02 — placeholder-clean contract check_values (lint-contract-check-values)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF02PlaceholderCleanContract:
    def test_no_hardcoded_pr_integer_in_any_contract_check_value(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path)

        values = _contract_check_values(clone_root / "contracts" / "OMN-9999.yaml")
        assert values, "contract must declare at least one check_value"
        for cv in values:
            assert not _HARDCODED_PR_NUMBER_RE.search(cv), (
                f"contract check_value carries a hardcoded integer PR number and "
                f"would fail lint-contract-check-values: {cv!r}"
            )
            assert "${PR_NUMBER}" in cv, (
                f"contract check_value must use ${{PR_NUMBER}} placeholder form, "
                f"got: {cv!r}"
            )
            assert "${REPO}" in cv, (
                f"contract check_value must use ${{REPO}} placeholder form, got: {cv!r}"
            )


# ---------------------------------------------------------------------------
# F-03 — yamlfmt-idempotent generated YAML (hosted yamlfmt Pre-commit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF03YamlfmtClean:
    def test_every_generated_yaml_is_yamlfmt_idempotent(self, tmp_path: Path) -> None:
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")

        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path)

        conf = tmp_path / ".yamlfmt"
        conf.write_text(_OCC_YAMLFMT_CONF)
        generated = sorted(clone_root.rglob("*.yaml"))
        assert generated, "emit must produce YAML artifacts"

        result = subprocess.run(
            [yamlfmt, "-lint", "-conf", str(conf), *[str(p) for p in generated]],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            "generated YAML is not yamlfmt-clean and would fail the hosted "
            f"yamlfmt Pre-commit:\n{result.stdout}\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# F-17 — closed / draft / do-not-merge suppression
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF17Suppression:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"state": "closed"}, "PR_CLOSED"),
            ({"draft": True}, "PR_DRAFT"),
            (
                {"title": "feat(OMN-9999): probe [WS4 PARITY PROBE - DO NOT MERGE]"},
                "PR_DO_NOT_MERGE",
            ),
            (
                {"labels": [{"name": "do-not-merge"}]},
                "PR_DO_NOT_MERGE",
            ),
        ],
    )
    def test_unmergeable_pr_is_suppressed_with_zero_side_effects(
        self,
        tmp_path: Path,
        overrides: dict[str, object],
        reason: str,
    ) -> None:
        pr_data = _default_pr_data()
        pr_data.update(overrides)
        emitter = OccCompanionEmitter()

        def fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            if path.endswith("/pulls/321"):
                return dict(pr_data)
            raise AssertionError(f"no REST call expected after suppression: {path}")

        # If suppression fails, the flow proceeds to author — which calls the
        # mocked clone. That tripwire (and the fake_rest AssertionError on any OCC
        # call) proves ZERO side effects, alongside the reason-coded skip string.
        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
            patch.object(
                emitter,
                "_clone_and_branch",
                side_effect=AssertionError("must not clone"),
            ),
        ):
            action = emitter._emit_companion_sync("OmniNode-ai/omnimarket", 321, None)

        assert action.startswith(f"skip:{reason}"), action
        assert "suppressed" in action

    def test_open_non_draft_pr_is_not_suppressed(self, tmp_path: Path) -> None:
        # GREEN-anchor: a normal product PR still authors a companion (so the
        # suppression is not over-broad).
        emitter = OccCompanionEmitter()
        action, clone_root, _ = _run_emit(emitter, tmp_path)
        assert action.startswith("authored OCC companion")
        assert (clone_root / "contracts" / "OMN-9999.yaml").is_file()


# ---------------------------------------------------------------------------
# F-04 — pre-existing contract gets THIS PR's base rows + stays eligible
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF04PreExistingContract:
    _PRE_EXISTING = (
        "---\n"
        'schema_version: "1.0.0"\n'
        'ticket_id: "OMN-9999"\n'
        'title: "Autobind OCC evidence for OMN-9999"\n'
        'summary: "Pre-existing companion authored by an earlier PR."\n'
        "is_seam_ticket: false\n"
        "interface_change: false\n"
        "interfaces_touched: []\n"
        "evidence_requirements:\n"
        '  - kind: "ci"\n'
        '    description: "earlier row"\n'
        '    command: "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"\n'
        "emergency_bypass:\n"
        "  enabled: false\n"
        '  justification: ""\n'
        '  follow_up_ticket_id: ""\n'
        "dod_evidence:\n"
        '  - id: "dod-earlier-pr-1"\n'
        '    description: "earlier PR row (a different PR of the same ticket)."\n'
        '    source: "generated"\n'
        "    checks:\n"
        '      - check_type: "command"\n'
        '        check_value: "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"\n'
    )

    # A PASS receipt for the earlier row (the earlier PR's merged receipt), so the
    # pre-existing companion is itself eligible before this PR appends its rows.
    _EARLIER_RECEIPT = (
        "---\n"
        'schema_version: "1.0.0"\n'
        'ticket_id: "OMN-9999"\n'
        'evidence_item_id: "dod-earlier-pr-1"\n'
        'check_type: "command"\n'
        'check_value: "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"\n'
        'contract_sha256: "sha256:' + "a" * 64 + '"\n'
        "status: PASS\n"
        'run_timestamp: "2026-07-01T00:00:00Z"\n'
        'commit_sha: "' + "d" * 40 + '"\n'
        'runner: "node_pr_lifecycle_fix_effect"\n'
        'verifier: "occ-evidence-source-autobind"\n'
        'probe_command: "gh pr view 1 --repo OmniNode-ai/omnimarket --json files"\n'
        "probe_stdout: |\n"
        '  {"files":[{"path":"src/x.py"}]}\n'
        'actual_output: "PASS: earlier row."\n'
        "exit_code: 0\n"
        'branch: "auto/omninode-ai-omnimarket-pr-1-occ-autobind"\n'
        "pr_number: 1\n"
    )

    def _preseed(self, clone_dir: Path) -> None:
        contract = clone_dir / "contracts" / "OMN-9999.yaml"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text(self._PRE_EXISTING)
        earlier = (
            clone_dir
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-earlier-pr-1"
            / "command.yaml"
        )
        earlier.parent.mkdir(parents=True, exist_ok=True)
        earlier.write_text(self._EARLIER_RECEIPT)

    def test_base_rows_appended_and_receipts_bind(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path, preseed=self._preseed)

        contract = clone_root / "contracts" / "OMN-9999.yaml"
        data = yaml.safe_load(contract.read_text())
        ids = {item["id"] for item in data["dod_evidence"]}
        # The pre-existing row is preserved AND this PR's base rows were appended.
        assert "dod-earlier-pr-1" in ids
        assert "dod-OmniNode-ai-omnimarket-pr-321" in ids
        assert "dod-OmniNode-ai-omnimarket-pr-321-ci" in ids

        # This PR's downstream receipt binds to a DECLARED entry — no PENDING
        # per-entry hash (the OCC#4304 break was PENDING/ineligible).
        downstream = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-OmniNode-ai-omnimarket-pr-321"
            / "command.yaml"
        )
        text = downstream.read_text()
        assert "PENDING" not in text
        expected = compute_contract_entry_sha256(
            data, "dod-OmniNode-ai-omnimarket-pr-321"
        )
        assert f'contract_entry_sha256: "{expected}"' in text

    def test_pre_existing_companion_is_occ_merge_eligible(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path, preseed=self._preseed)
        snapshot = ModelOccEligibilityInput(
            repo="onex_change_control",
            pr_number=55,
            pr_title=(
                "evidence(OMN-9999): OCC Evidence-Source autobind for "
                "OmniNode-ai/omnimarket#321"
            ),
            pr_body="Autobind OCC evidence.\n\nEvidence-Ticket: OMN-9999\n",
            pr_branch="auto/omninode-ai-omnimarket-pr-321-occ-autobind",
            pr_commit_shas=("c" * 40,),
            pr_commit_texts=(
                "evidence(OMN-9999): autobind OmniNode-ai/omnimarket#321",
            ),
            occ_commit_sha="c" * 40,
            contracts_dir=clone_root / "contracts",
            receipts_dir=clone_root / "drift" / "dod_receipts",
        )
        result = validate_occ_merge_eligibility(snapshot)
        assert result.eligible is True, result.detail
        assert result.reason is EnumOccEligibilityReason.ELIGIBLE


# ---------------------------------------------------------------------------
# F-01 — append-only: a prior MERGED receipt is never mutated by a later emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestF01AppendOnly:
    # A prior merged receipt for a DIFFERENT PR of the SAME ticket, carrying a
    # whole-file contract_sha256 pinned to the contract as it was when that PR
    # merged. A later PR appends a dod_evidence item, changing the whole-file
    # hash — the pre-fix rglob rebind rewrote this receipt's hash (mutating an
    # already-merged file); the scoped rebind must leave it byte-for-byte intact.
    _PRIOR_RECEIPT = (
        "---\n"
        'schema_version: "1.0.0"\n'
        'ticket_id: "OMN-9999"\n'
        'evidence_item_id: "dod-earlier-pr-1"\n'
        'check_type: "command"\n'
        'check_value: "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"\n'
        'contract_sha256: "sha256:' + "a" * 64 + '"\n'
        "status: PASS\n"
        'run_timestamp: "2026-07-01T00:00:00Z"\n'
        'commit_sha: "' + "d" * 40 + '"\n'
        'runner: "node_pr_lifecycle_fix_effect"\n'
        'verifier: "occ-evidence-source-autobind"\n'
        'branch: "auto/omninode-ai-omnimarket-pr-1-occ-autobind"\n'
        "pr_number: 1\n"
    )

    def _preseed(self, clone_dir: Path) -> None:
        # Pre-existing contract (so authoring appends this PR's rows) that ALSO
        # declares the earlier PR's item, plus the earlier PR's merged receipt.
        contract = clone_dir / "contracts" / "OMN-9999.yaml"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text(TestF04PreExistingContract._PRE_EXISTING)
        prior = (
            clone_dir
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-earlier-pr-1"
            / "command.yaml"
        )
        prior.parent.mkdir(parents=True, exist_ok=True)
        prior.write_text(self._PRIOR_RECEIPT)

    def test_prior_merged_receipt_is_not_mutated(self, tmp_path: Path) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path, preseed=self._preseed)
        prior = (
            clone_root
            / "drift"
            / "dod_receipts"
            / "OMN-9999"
            / "dod-earlier-pr-1"
            / "command.yaml"
        )
        assert prior.read_text() == self._PRIOR_RECEIPT, (
            "a prior MERGED receipt for a different PR of the same ticket was "
            "mutated by a later emit — the OCC#4293/4295/4296 append-only break"
        )

    # Backstop guard tests — the emitter's git call (`_run_git` -> `git diff
    # --name-status`) is mocked so the guard's parse/decision logic is tested
    # without spawning a nested git repo. (A real nested `git init`/`git commit`
    # in a unit test is hook-unsafe: run inside `git commit`'s pre-commit hook it
    # inherits GIT_DIR/GIT_INDEX_FILE and clobbers the outer worktree index.)
    _EID = "dod-OmniNode-ai-omnimarket-pr-321"

    def _guard(self, diff_output: str) -> None:
        emitter = OccCompanionEmitter()
        allowed = emitter._allowed_paths(["OMN-9999"], {self._EID})
        with patch.object(emitter, "_run_git", return_value=diff_output):
            emitter._assert_append_only(Path("/tmp/occ"), "0" * 40, allowed)

    def test_append_only_guard_rejects_out_of_set_modify(self) -> None:
        # A modify of a file OUTSIDE this run's set (a foreign contract) is rejected.
        with pytest.raises(RuntimeError, match="append-only violation"):
            self._guard("M\tcontracts/OMN-OTHER.yaml")

    def test_append_only_guard_rejects_deletion(self) -> None:
        # Any deletion — even of an in-set path — is rejected.
        with pytest.raises(RuntimeError, match="append-only violation"):
            self._guard(f"D\tdrift/dod_receipts/OMN-9999/{self._EID}/command.yaml")

    def test_append_only_guard_accepts_in_set_changes(self) -> None:
        # Adding ONLY this run's contract + receipt is allowed (must not raise).
        self._guard(
            "A\tcontracts/OMN-9999.yaml\n"
            f"A\tdrift/dod_receipts/OMN-9999/{self._EID}/command.yaml"
        )
