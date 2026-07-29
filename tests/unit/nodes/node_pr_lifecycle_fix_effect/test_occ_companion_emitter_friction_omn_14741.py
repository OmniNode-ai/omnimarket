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
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"


# ---------------------------------------------------------------------------
# OMN-15317 — this suite asserts the LEGACY (pr_existence) companion bytes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``OMNI_OCC_CHECK_BINDING=pr_existence`` for every test in this module.

    OMN-15317 flipped the producer default to ``content_bound``. These tests
    assert byte-for-byte properties of the legacy check shape and drive
    ``_emit_companion_sync`` WITHOUT injecting a RED-derivable diff or faking
    ``_execute_probe_raw``, so under the new default every one of them would
    (correctly) take the fail-closed ``skip:NO_RED_DERIVABLE_CHECK`` branch and
    assert nothing about the property it was written for.

    Pinning is explicit rather than implicit: the properties covered here
    (F-01 append-only, F-02 placeholder-clean, F-03 yamlfmt idempotence, F-04
    pre-existing-contract append, F-06/F-16 probe parity, F-17 suppression, the
    OMN-14793 lease) are binding-orthogonal. The CONTENT-BOUND default path is
    driven end to end in the same blocking gate by
    ``test_occ_autobind_contention_omn_15247.py``
    (``TestShippedDefaultIsContentBound`` / ``TestContentBoundChecks``), which
    injects the probe results instead of reaching the network.
    """
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


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
    lease_acquire: Callable[..., bool] | None = None,
    lease_release: MagicMock | None = None,
    open_or_sync_side_effect: BaseException | None = None,
) -> tuple[str, Path, list[list[str]]]:
    """Drive the REAL ``_emit_companion_sync`` with a temp clone + mocked I/O.

    git + the OCC-PR-open + product-PR-patch + probe are mocked; the contract +
    receipt rendering, structural appends, file writes, and contract_sha256 rebind
    run for real so the emitted byte-shape is exercised end to end. ``preseed`` is
    a callback ``(clone_dir) -> None`` invoked after the (mocked) clone mkdir so a
    test can plant a pre-existing contract or a prior merged receipt.

    OMN-14793: the single-producer lease is always-on, so it is mocked here too.
    ``lease_acquire`` overrides ``acquire_occ_companion_lease`` (default: grant the
    lease) — a test drives it to prove the second concurrent producer is rejected,
    or raises from it to prove fail-closed. ``lease_release`` lets a test assert the
    lease is freed in the mint's ``finally``. ``open_or_sync_side_effect`` injects a
    mid-mint failure to exercise that ``finally``.
    """
    clone_root = tmp_path / "onex_change_control"
    git_calls: list[list[str]] = []
    resolved_pr_data = pr_data if pr_data is not None else _default_pr_data()
    acquire: Callable[..., bool] = (
        lease_acquire if lease_acquire is not None else (lambda **_kw: True)
    )
    release_target = lease_release if lease_release is not None else MagicMock()

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

    if open_or_sync_side_effect is not None:
        open_or_sync_patch = patch.object(
            emitter, "_open_or_sync_occ_pr", side_effect=open_or_sync_side_effect
        )
    else:
        open_or_sync_patch = patch.object(
            emitter, "_open_or_sync_occ_pr", return_value=55
        )

    with (
        patch(f"{_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_MOD}._resolve_github_token", return_value="fake-token"),
        patch(f"{_MOD}.acquire_occ_companion_lease", side_effect=acquire),
        patch(f"{_MOD}.release_occ_companion_lease", release_target),
        patch.object(emitter, "_run_git", side_effect=fake_run_git),
        patch.object(emitter, "_clone_and_branch", side_effect=fake_clone),
        open_or_sync_patch,
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


# ---------------------------------------------------------------------------
# F-06 — runtime probe is the GraphQL `gh pr view --json files` (OMN-14766)
# ---------------------------------------------------------------------------


def _receipt_field(receipt_path: Path, field: str) -> str:
    data = yaml.safe_load(receipt_path.read_text())
    return str(data.get(field, ""))


@pytest.mark.unit
class TestF06RuntimeProbeIsGraphQL:
    """The emitter's product-diff RUNTIME probe must observe the SAME surface the
    declared check_value asserts over, and must never be the REST-fragile
    `gh pr diff ... --name-only` (OCC#4297 HTML/503).

    OMN-15247 R21 restates F-06's target: the surface is
    `gh api repos/<repo>/pulls/<n>/files`, not `gh pr view --json files`. The old
    spelling is refused by the OMN-15309 predicate as NOT_EXECUTED (bare `gh`),
    and `probe_command == check_value` can no longer hold byte-for-byte because
    the check_value carries the `| grep` assertion while the probe must stay
    pipe-free (the emitter shlex-splits and executes it). The invariant is
    therefore the strictly stronger one it was standing in for: the check_value
    IS the probe_command plus its assertion."""

    _CI_RECEIPT = (
        "drift/dod_receipts/OMN-9999/dod-OmniNode-ai-omnimarket-pr-321-ci/command.yaml"
    )

    def test_ci_receipt_probe_command_is_graphql_json_files(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path)
        receipt = clone_root / self._CI_RECEIPT
        probe = _receipt_field(receipt, "probe_command")
        assert probe.startswith("gh api repos/"), (
            f"F-06: CI receipt probe_command must be a `gh api` read, got: {probe!r}"
        )
        assert "/pulls/321/files" in probe, (
            f"F-06: CI receipt probe_command must read the PR file list, got: {probe!r}"
        )
        assert "gh pr diff" not in probe, (
            f"F-06: CI receipt probe_command still uses the REST-fragile `gh pr "
            f"diff` (OCC#4297): {probe!r}"
        )
        assert "gh pr view" not in probe, (
            f"F-06/R21: CI receipt probe_command still uses the NOT_EXECUTED "
            f"`gh pr view` spelling: {probe!r}"
        )

    def test_ci_receipt_check_value_is_the_probe_plus_its_assertion(
        self, tmp_path: Path
    ) -> None:
        # The runtime probe and the re-run check_value must observe the SAME
        # surface: the check_value is the probe_command with a `| grep` assertion
        # appended. Anything else means the receipt records one fact and the
        # contract asserts another.
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(emitter, tmp_path)
        receipt = clone_root / self._CI_RECEIPT
        probe = _receipt_field(receipt, "probe_command")
        check = _receipt_field(receipt, "check_value")
        assert check.startswith(probe), (
            f"F-06: CI receipt check_value must extend its probe_command; "
            f"probe={probe!r} check={check!r}"
        )
        assert check[len(probe) :].lstrip().startswith("| grep"), (
            f"F-06: the extension must be the falsifiable grep assertion: {check!r}"
        )


# ---------------------------------------------------------------------------
# F-16 — private-repo companions emit hosted-safe receipt-local check_values
# ---------------------------------------------------------------------------


def _private_pr_data() -> dict[str, object]:
    data = _default_pr_data()
    data["base"] = {"repo": {"private": True}}
    return data


def _public_pr_data() -> dict[str, object]:
    data = _default_pr_data()
    data["base"] = {"repo": {"private": False}}
    return data


def _dod_item_check_values(contract_path: Path, item_id: str) -> list[str]:
    data = yaml.safe_load(contract_path.read_text())
    out: list[str] = []
    for item in data.get("dod_evidence") or []:
        if item.get("id") != item_id:
            continue
        for check in item.get("checks") or []:
            cv = check.get("check_value")
            if isinstance(cv, str):
                out.append(cv)
    return out


@pytest.mark.unit
class TestF16PrivateRepoHostedSafe:
    """A private product repo cannot be re-probed by the hosted OCC runner (token
    scope -- the item-13 gap recorded on OCC#5406).

    OMN-15247 R21 replaces F-16's MECHANISM, not its goal. The receipt-local
    `grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/...` form it
    used to emit is refused UNCONDITIONALLY by the OMN-15309 predicate as
    INSIDE_OWN_DIFF, which is precisely why the three companions minted for the
    one private product repo (OCC#5406 / #5415 / #5418) were born BLOCKED. The
    goal is met instead by the PLACEHOLDER form: `${REPO}` / `${PR_NUMBER}` are
    pre-substituted by the compliance runner with the repo/PR whose CI is
    executing, so the hosted OCC job never dereferences the private repo at all.
    """

    _EID = "dod-OmniNode-ai-omnimarket-pr-321"
    _CI_EID = "dod-OmniNode-ai-omnimarket-pr-321-ci"
    _CONTRACT = "contracts/OMN-9999.yaml"
    _DOWN_RECEIPT = (
        "drift/dod_receipts/OMN-9999/dod-OmniNode-ai-omnimarket-pr-321/command.yaml"
    )
    _CI_RECEIPT = (
        "drift/dod_receipts/OMN-9999/dod-OmniNode-ai-omnimarket-pr-321-ci/command.yaml"
    )

    @staticmethod
    def _is_receipt_local(cv: str) -> bool:
        """The pre-R21 self-referential form. Retained ONLY as a RED control."""
        return cv.startswith("grep -q '^status: PASS$'") and (
            "$CONTRACT_REPO_DIR/drift/dod_receipts/" in cv
        )

    def test_the_red_control_matches_the_pre_r21_form(self) -> None:
        # feedback_prove_red_against_exists_but_wrong: the assertions below are
        # only load-bearing if this predicate matches the exact string the
        # producer used to emit for a private repo.
        assert self._is_receipt_local(
            "grep -q '^status: PASS$' "
            "$CONTRACT_REPO_DIR/drift/dod_receipts/OMN-9999/dod-x/command.yaml"
        )

    def test_private_repo_items_never_dereference_the_private_repo(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(
            emitter, tmp_path, pr_data=_private_pr_data()
        )
        contract = clone_root / self._CONTRACT
        for item_id in (self._EID, self._CI_EID):
            values = _dod_item_check_values(contract, item_id)
            assert values, f"private-repo contract must declare item {item_id}"
            for cv in values:
                # The R21 invariant: placeholder form, so the executing job's own
                # token always suffices and the private repo is never named.
                placeholder_msg = (
                    f"F-16/R21: private-repo item {item_id} must use the "
                    f"placeholder form, got: {cv!r}"
                )
                assert "${REPO}" in cv, placeholder_msg
                assert "${PR_NUMBER}" in cv, placeholder_msg
                assert "omninode_infra" not in cv, (
                    f"F-16: private-repo item {item_id} dereferences the private "
                    f"repo the hosted OCC token cannot read: {cv!r}"
                )
                # And it must NOT be the self-referential form R21 removed.
                assert not self._is_receipt_local(cv), (
                    f"F-16/R21: private-repo item {item_id} regressed to the "
                    f"INSIDE_OWN_DIFF receipt grep: {cv!r}"
                )
                assert "gh pr view" not in cv, cv
                assert "gh pr diff" not in cv, cv

    def test_private_repo_receipts_keep_the_live_cross_repo_probe(
        self, tmp_path: Path
    ) -> None:
        emitter = OccCompanionEmitter()
        _action, clone_root, _ = _run_emit(
            emitter, tmp_path, pr_data=_private_pr_data()
        )
        for rel in (self._DOWN_RECEIPT, self._CI_RECEIPT):
            receipt = clone_root / rel
            # A receipt records what the emitter (which HAS repo scope) actually
            # ran, and is never re-executed by the hosted runner -- so it names
            # the real repo. That is the provenance F-16 always preserved.
            probe = _receipt_field(receipt, "probe_command")
            assert probe.startswith("gh api repos/"), probe
            assert "/files" in probe, probe
            # The receipt's own check_value must not be the self-referential form.
            assert not self._is_receipt_local(_receipt_field(receipt, "check_value")), (
                rel
            )

    def test_public_and_private_declare_the_same_admissible_checks(
        self, tmp_path: Path
    ) -> None:
        # R21 collapses the public/private fork in the CONTRACT: both render the
        # identical placeholder vocabulary, so there is no private-repo downgrade
        # left to regress. The fork survives only for the LITERAL content-bound
        # pin, which is gated separately on repo visibility.
        pub_root = _run_emit(
            OccCompanionEmitter(), tmp_path / "pub", pr_data=_public_pr_data()
        )[1]
        priv_root = _run_emit(
            OccCompanionEmitter(), tmp_path / "priv", pr_data=_private_pr_data()
        )[1]
        for item_id in (self._EID, self._CI_EID):
            assert _dod_item_check_values(
                pub_root / self._CONTRACT, item_id
            ) == _dod_item_check_values(priv_root / self._CONTRACT, item_id)


# ---------------------------------------------------------------------------
# OMN-14793 (OMN-14783 rec #2) — single-producer enforcement lease
#
# Two live OccCompanionEmitter instances (the local merge_sweep mint path and the
# .201 effects lane) run on different hosts and force-push the SAME deterministic
# auto/* branch, so "branch exists" is no discriminator. The guard is an atomic
# create-if-absent lease keyed on the product PR head SHA: the first acquirer mints,
# a second concurrent producer no-ops with ZERO side effects. These fixtures drive
# the LIVE emitter and prove first-wins / second-rejected is load-bearing (RED vs
# EXISTS-but-WRONG), keyed on head, fail-closed, and released on failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFCSingleProducerLease:
    def test_second_concurrent_producer_no_ops_with_zero_side_effects(
        self, tmp_path: Path
    ) -> None:
        # F-C1: two producers for the same (repo, PR, head). The lease grants the
        # first and rejects the second; the second must return skip:LEASE_HELD and
        # perform NO git side effects (no clone/branch/commit/push/patch).
        grants = {"n": 0}

        def _acq(**_kw: object) -> bool:
            grants["n"] += 1
            return grants["n"] == 1  # first acquirer wins, second is rejected

        action1, _root1, git1 = _run_emit(
            OccCompanionEmitter(), tmp_path / "p1", lease_acquire=_acq
        )
        action2, root2, git2 = _run_emit(
            OccCompanionEmitter(), tmp_path / "p2", lease_acquire=_acq
        )

        assert action1.startswith("authored OCC companion"), action1
        assert git1, "first producer must perform the mint (clone/commit/push)"

        assert action2.startswith("skip:LEASE_HELD"), action2
        assert git2 == [], (
            "second producer must perform ZERO git side effects when the lease is "
            "held — no clone, no branch, no commit, no push"
        )
        assert not (root2 / "contracts").exists(), (
            "second producer must not materialise any companion tree"
        )

    def test_without_guard_second_producer_also_authors_red_vs_exists(
        self, tmp_path: Path
    ) -> None:
        # F-C2 (RED-vs-EXISTS-but-WRONG): with the lease ALWAYS granted (guard
        # reverted) the second producer DOES author. The ONLY difference from F-C1
        # is the lease decision, and it flips skip -> author — proving the reject in
        # F-C1 is load-bearing, not a vacuous pass.
        action1, _root1, git1 = _run_emit(
            OccCompanionEmitter(), tmp_path / "p1", lease_acquire=lambda **_kw: True
        )
        action2, root2, git2 = _run_emit(
            OccCompanionEmitter(), tmp_path / "p2", lease_acquire=lambda **_kw: True
        )

        assert action1.startswith("authored OCC companion"), action1
        assert action2.startswith("authored OCC companion"), action2
        # Both producers author when the lease is always granted — the guard's
        # False return is the ONLY thing that prevents dual authoring.
        assert git1, "first producer must author"
        assert git2, "second producer ALSO authors when the guard is reverted"
        assert (root2 / "contracts" / "OMN-9999.yaml").is_file()

    def test_synchronize_on_new_head_is_not_wedged_and_keys_on_head(
        self, tmp_path: Path
    ) -> None:
        # F-C3: a legitimate `synchronize` re-fire on a NEW head SHA yields a new
        # lease key (keyed on PR + head), so it is granted and proceeds — the guard
        # does not block a real re-mint after new commits. Also asserts the lease is
        # keyed on the CURRENT head, not the branch or a stale head.
        seen: dict[str, object] = {}

        def _acq(**kw: object) -> bool:
            seen.update(kw)
            return True

        new_head = "e" * 40
        pr_data = _default_pr_data()
        pr_data["head"] = {"sha": new_head, "ref": "feature-branch"}

        action, _root, git_calls = _run_emit(
            OccCompanionEmitter(), tmp_path, pr_data=pr_data, lease_acquire=_acq
        )

        assert action.startswith("authored OCC companion"), action
        assert git_calls, "a re-fire on a NEW head must proceed to mint"
        assert seen["head_sha"] == new_head, (
            "the lease must be keyed on the CURRENT product PR head SHA"
        )
        assert seen["pr_number"] == 321

    def test_lease_released_on_mid_mint_failure(self, tmp_path: Path) -> None:
        # F-C5: an exception mid-mint must still release the lease in the `finally`,
        # so a failed attempt does not wedge the head until the TTL steal fires. The
        # original exception must still propagate.
        release_mock = MagicMock()
        with pytest.raises(RuntimeError, match="boom"):
            _run_emit(
                OccCompanionEmitter(),
                tmp_path,
                lease_release=release_mock,
                open_or_sync_side_effect=RuntimeError("boom"),
            )
        release_mock.assert_called_once()

    def test_fail_closed_on_lease_acquire_transport_error(self, tmp_path: Path) -> None:
        # F-C6: if the lease surface is unreachable (a non-422 transport error),
        # acquisition fails CLOSED — the emitter raises and performs NO clone/author
        # rather than silently falling through to dual authoring.
        clone_root = tmp_path / "onex_change_control"

        def _boom(**_kw: object) -> bool:
            raise GitHubApiError("lease surface unreachable", status_code=503)

        with pytest.raises(GitHubApiError):
            _run_emit(OccCompanionEmitter(), tmp_path, lease_acquire=_boom)
        assert not clone_root.exists(), (
            "fail-closed: an unreachable lease surface must not clone or author"
        )
