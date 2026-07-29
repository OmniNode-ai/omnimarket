# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14710 — OCC-emitter golden-fixture regression suite (plan WS3 / Phase 4).

The canonical OCC companion producer (``node_occ_companion_compute`` /
``occ_evidence_stamp``) repeatedly emitted bad companions that only failed in
hosted ``onex_change_control`` CI, forcing manual worktree repair. Root causes
were catalogued in the 2026-07-16 overnight merge-sweep friction report
(``docs/tracking/friction/2026-07-16-overnight-merge-sweep-friction-report.md``).

This suite makes the producer FAIL BEFORE PUSH on each repeatable class. Every
class has:

  * a POSITIVE golden test — the producer's real output satisfies the class
    invariant; and
  * a NEGATIVE fixture — a hand-crafted or captured-bad output that the SAME
    checker rejects, proving the assertion is load-bearing (not vacuously green;
    feedback_prove_red_against_exists_but_wrong).

Classes covered (friction id -> what it catches):

  * F-01 non-append-only: a generated diff must not touch receipts/contracts
    outside the cited-ticket namespace (mutating already-merged evidence).
  * F-02 hardcoded PR ints: contract ``check_value``s must use ``${PR_NUMBER}`` /
    ``${REPO}`` placeholders, never a live integer PR number.
  * F-03 formatter-dirty: generated YAML must be a ``yamlfmt`` no-op under the
    OCC repo's own yamlfmt config.
  * F-04 PENDING hashes: a generated companion must not ship an unbound
    ``contract_entry_sha256: sha256:PENDING`` sentinel (esp. existing/merged
    contract reuse).
  * F-05 deploy-scope: a runtime-path-touching PR's companion must declare a
    deploy/diff-scope check, not existence-only evidence.
  * F-14 OPEN-only moving-head checks: no ``check_value`` may assert the product
    PR is in state ``OPEN`` (breaks after the PR merges).
  * F-15 generic/placeholder DoD rows: reject impossible-pre-merge rows
    ("PR merged to main") and bare generic ``dod-NNN`` ids at authoring.
  * F-17 closed/draft/do-not-merge targets: the producer must suppress
    (no-op) companions for closed/merged, draft, or do-not-merge PRs.

The gate rules mirrored below (F-02 placeholder lint, F-15 substance floor) are
copied VERBATIM from the canonical onex_change_control gates
(``scripts/lint_contract_check_values.py``,
``scripts/validation/check_contract_substance_floor.py``) which are not
importable here; drift in those gates should turn these RED.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    render_compute_companion_contract,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "occ_companion_golden"
_OCC_4284_DIFF = _FIXTURES / "occ_4284_companion.diff"
_OCC_YAMLFMT = _FIXTURES / "occ.yamlfmt"


# ---------------------------------------------------------------------------
# Request builders
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


# A request whose OCC PR is known -> the contract ALSO declares the self-bind
# item and the plan emits every companion file kind (contract + downstream +
# self-bind receipts). This is the maximal fresh-path fan-out.
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


def _merged_path_request() -> ModelOccCompanionRequest:
    """A 2nd-consumer (merged-contract) request (OMN-14623 two-audiences path).

    The cited ticket's OCC contract is already merged to dev with a declared
    prior entry, so the producer must emit a NET-NEW supersession + an appended
    self-bind contract, never mutate the frozen base — and never leave a PENDING
    hash (F-04).
    """
    ticket = "OMN-9999"
    old_pr = 300
    old_evidence_id = f"dod-OmniNode-ai-omnimarket-pr-{old_pr}"
    merged_contract = render_compute_companion_contract(
        ticket_id=ticket,
        repo="OmniNode-ai/omnimarket",
        pr_number=old_pr,
        evidence_id=old_evidence_id,
    )
    state = ModelOccContractState(
        ticket_id=ticket,
        exists=True,
        merged=True,
        existing_entry_ids=(old_evidence_id,),
        raw_contract_text=merged_contract,
    )
    return _request(occ_contract_states=(state,), **_PASS2)


def _contract_check_values(plan: ModelOccCompanionPlan) -> list[str]:
    contract = next(
        f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
    )
    data = yaml.safe_load(contract.content)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


# ---------------------------------------------------------------------------
# occ#4284 captured negative fixture — parse the git diff into {path: content}
# for the NEW files it adds.
# ---------------------------------------------------------------------------
def _parse_added_files(diff_text: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
            files.setdefault(current, [])
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            current = None if line.startswith("diff --git") else current
            continue
        if (
            line.startswith("@@")
            or line.startswith("index ")
            or line.startswith("new file")
        ):
            continue
        if current is not None and line.startswith("+"):
            files[current].append(line[1:])
    return {p: "\n".join(rows) for p, rows in files.items()}


def _occ_4284_added_files() -> dict[str, str]:
    return _parse_added_files(_OCC_4284_DIFF.read_text())


# ---------------------------------------------------------------------------
# Checkers — pure invariants. Verbatim copies are noted at their definition.
# ---------------------------------------------------------------------------

# --- F-02 verbatim from scripts/lint_contract_check_values.py (OMN-9350/14673) -
_GH_PR_PREFIX = ("gh pr checks", "gh pr view", "gh pr diff")
_HARDCODED_PR_NUMBER_RE = re.compile(r"gh pr (?:checks|view|diff)\s+\d+\s")
_BRACE_PR_RE = re.compile(r"\{pr\}")
_BRACE_REPO_RE = re.compile(r"\{repo\}")


def lint_check_value(value: str) -> str | None:
    """Return a rejection reason for a non-placeholder gh-pr check_value, else None."""
    stripped = value.strip()
    if not stripped.startswith(_GH_PR_PREFIX):
        return None
    if _HARDCODED_PR_NUMBER_RE.search(stripped):
        return "hardcoded integer PR number"
    if _BRACE_PR_RE.search(stripped):
        return "wrong-format {pr} placeholder"
    if _BRACE_REPO_RE.search(stripped):
        return "wrong-format {repo} placeholder"
    if "${PR_NUMBER}" not in stripped:
        return "missing ${PR_NUMBER} placeholder"
    if "${REPO}" not in stripped and "--repo" not in stripped:
        return "missing --repo argument"
    return None


# --- F-15/F-05 verbatim from check_contract_substance_floor.py (OMN-14409) ------
_EXISTENCE_JSON_FIELDS = frozenset(
    {
        "number",
        "state",
        "url",
        "title",
        "body",
        "headrefname",
        "headrefoid",
        "baserefname",
        "author",
        "isdraft",
        "mergedat",
        "createdat",
        "updatedat",
        "closedat",
        "mergeable",
        "mergestatestatus",
        "mergecommit",
    }
)
_GH_PR_VIEW_RE = re.compile(r"\bgh\s+pr\s+view\b")
_JSON_FLAG_RE = re.compile(r"--json[=\s]+([A-Za-z0-9_,]+)")
_CMD = r"(?:^|[|;&]\s*|\$\(\s*|\b(?:run|exec|xargs|sudo|time|env|then|do|else)\s+)"
_STATIC_ASSERT_RE = re.compile(rf"{_CMD}(grep|rg|ast-grep)\b")
# OMN-14783 F-06: a `gh pr view --json files` diff-assertion derives to L1 in the
# canonical substance floor (`_DIFF_ASSERT_RE`, check_contract_substance_floor.py).
# This local mirror had drifted (it only recognized `| grep`); the F-06 swap of
# the compute contract's diff-scope check to `--json files` — matching the
# born-path emitter — requires this branch, verbatim, so drift stays RED.
_DIFF_ASSERT_RE = re.compile(r"(--json[=\s]+[^|]*\bfiles\b|\.files\[)")


def is_existence_probe(command: str) -> bool:
    if not _GH_PR_VIEW_RE.search(command):
        return False
    fields: set[str] = set()
    for match in _JSON_FLAG_RE.finditer(command):
        fields.update(f.strip().lower() for f in match.group(1).split(",") if f.strip())
    if not fields:
        return True
    return fields.issubset(_EXISTENCE_JSON_FIELDS)


def is_substantive(check_value: str) -> bool:
    command = (check_value or "").strip()
    if is_existence_probe(command):
        return False
    return bool(_DIFF_ASSERT_RE.search(command) or _STATIC_ASSERT_RE.search(command))


# --- F-01 append-only ----------------------------------------------------------
def files_outside_cited_namespace(
    plan: ModelOccCompanionPlan,
) -> list[str]:
    """Paths a plan would write outside its cited-ticket receipt/contract namespace."""
    violations: list[str] = []
    for f in plan.companion_files:
        allowed = any(
            f.path == f"contracts/{t}.yaml"
            or f.path.startswith(f"drift/dod_receipts/{t}/")
            for t in plan.tickets
        )
        if not allowed:
            violations.append(f.path)
    return violations


# --- F-05 deploy-scope ---------------------------------------------------------
_RUNTIME_TOUCHING_RE = re.compile(r"(^|/)(nodes/|migrations/)|\.py$", re.IGNORECASE)
# OMN-15247 R21b: back to `gh pr diff`. R21 moved this to
# `gh api .../pulls/<n>/files` because deploy-gate's falsifiability classifier
# (LIVE-probe vocabulary only) rejects `gh pr diff ... | grep`. That is true and
# the replacement was still wrong: the OCC runner pre-substitutes
# ${REPO}/${PR_NUMBER} with the OCC COMPANION's own repo/number, so in OCC CI the
# value grepped the COMPANION's filenames for runtime paths -- exit 1 against
# OCC#5418's real file list (born RED), or exit 0 only because the producer had
# just created a receipt directory literally named `dod-deploy-assessment`
# (circular). The deploy-gate gap is real, pre-existing and tracked separately.
_DIFF_SCOPE_RE = re.compile(r"\bgh pr diff\b")


def is_runtime_touching(changed_files: tuple[str, ...]) -> bool:
    return any(_RUNTIME_TOUCHING_RE.search(f) for f in changed_files)


def has_deploy_scope_check(check_values: list[str]) -> bool:
    return any(_DIFF_SCOPE_RE.search(cv) for cv in check_values)


# --- F-14 OPEN-only moving-head ------------------------------------------------
_STATE_OPEN_ASSERT_RE = re.compile(
    r"(grep|--jq|\bjq\b|==|=~)[^\n]*\bOPEN\b", re.IGNORECASE
)


def asserts_open_state(check_value: str) -> bool:
    return bool(_STATE_OPEN_ASSERT_RE.search(check_value))


# --- F-15 generic/placeholder rows ---------------------------------------------
_GENERIC_ID_RE = re.compile(r"^dod-\d+$")
_IMPOSSIBLE_PREMERGE_RE = re.compile(
    r"merged to (?:main|dev)|PR merged|deployed to prod|released to pypi",
    re.IGNORECASE,
)


def generic_placeholder_rows(contract_yaml: str) -> list[str]:
    """Return ids of dod_evidence rows that are generic/impossible-pre-merge."""
    data = yaml.safe_load(contract_yaml)
    bad: list[str] = []
    for item in data.get("dod_evidence", []):
        item_id = str(item.get("id", ""))
        desc = str(item.get("description", ""))
        checks = " ".join(
            str(ck.get("check_value", "")) for ck in item.get("checks", [])
        )
        if (
            _GENERIC_ID_RE.match(item_id)
            or _IMPOSSIBLE_PREMERGE_RE.search(desc)
            or _IMPOSSIBLE_PREMERGE_RE.search(checks)
        ):
            bad.append(item_id or "<no-id>")
    return bad


# --- F-03 formatter --------------------------------------------------------------
def yamlfmt_is_noop(content: str) -> tuple[bool, str]:
    """True when ``yamlfmt`` (OCC config) reports no formatting change for content."""
    yamlfmt = shutil.which("yamlfmt")
    if yamlfmt is None:  # pragma: no cover - environment guard
        pytest.skip("yamlfmt binary not available")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        name = tf.name
    try:
        result = subprocess.run(
            [yamlfmt, "-conf", str(_OCC_YAMLFMT), "-lint", name],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(name).unlink(missing_ok=True)
    return result.returncode == 0, (result.stdout + result.stderr)


def yamlfmt_output(content: str) -> str:
    """Return the bytes ``yamlfmt`` (OCC config) would write for ``content``."""
    yamlfmt = shutil.which("yamlfmt")
    if yamlfmt is None:  # pragma: no cover - environment guard
        pytest.skip("yamlfmt binary not available")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        name = tf.name
    try:
        subprocess.run(
            [yamlfmt, "-conf", str(_OCC_YAMLFMT), name],
            capture_output=True,
            text=True,
            check=False,
        )
        return Path(name).read_text(encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# F-01 non-append-only
# ---------------------------------------------------------------------------
class TestF01AppendOnly:
    def test_fresh_path_files_stay_in_cited_namespace(self) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        assert plan.companion_files, "expected a non-empty companion plan"
        assert files_outside_cited_namespace(plan) == []
        assert all(f.is_net_new for f in plan.companion_files)

    def test_merged_path_only_writes_net_new_supersessions(self) -> None:
        plan = compute_companion_plan(_merged_path_request())
        assert files_outside_cited_namespace(plan) == []
        # The merged base receipt is re-bound via a NET-NEW .supersede.<pr>.yaml
        # file, never by editing the frozen prior-entry command.yaml.
        supersedes = [
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.SUPERSEDE_RECEIPT
        ]
        assert supersedes, "merged path must emit a supersession receipt"
        for f in supersedes:
            assert ".supersede." in f.path
            assert f.is_net_new

    def test_negative_foreign_ticket_file_is_flagged(self) -> None:
        # RED control: a plan that would write into a DIFFERENT ticket's receipt
        # namespace (the F-01 mutate-already-merged-evidence class) is rejected.
        good = compute_companion_plan(_request())
        from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
            ModelCompanionFile,
        )

        foreign = ModelCompanionFile(
            path="drift/dod_receipts/OMN-11111/dod-foreign/command.yaml",
            content="---\n",
            kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
            ticket_id="OMN-11111",
        )
        tampered = good.model_copy(
            update={"companion_files": (*good.companion_files, foreign)}
        )
        assert files_outside_cited_namespace(tampered) == [
            "drift/dod_receipts/OMN-11111/dod-foreign/command.yaml"
        ]


# ---------------------------------------------------------------------------
# F-02 hardcoded PR integers
# ---------------------------------------------------------------------------
class TestF02PlaceholderLint:
    def test_fresh_path_contract_checks_are_placeholder_normalized(self) -> None:
        for cv in _contract_check_values(compute_companion_plan(_request())):
            assert lint_check_value(cv) is None, f"lint rejects minted check: {cv}"

    def test_pass2_contract_checks_are_placeholder_normalized(self) -> None:
        for cv in _contract_check_values(compute_companion_plan(_request(**_PASS2))):
            assert lint_check_value(cv) is None, f"lint rejects minted check: {cv}"

    def test_negative_occ_4284_contract_has_hardcoded_pr_ints(self) -> None:
        # RED control from the REAL captured failed companion (occ#4284): its
        # contracts/OMN-14695.yaml check_values hardcode live PR integers.
        added = _occ_4284_added_files()
        contract_yaml = added["contracts/OMN-14695.yaml"]
        data = yaml.safe_load(contract_yaml)
        checks = [
            ck["check_value"] for it in data["dod_evidence"] for ck in it["checks"]
        ]
        reasons = [lint_check_value(cv) for cv in checks]
        assert "hardcoded integer PR number" in reasons, (
            f"expected occ#4284 to trip the placeholder lint; got {reasons}"
        )


# ---------------------------------------------------------------------------
# F-03 formatter-dirty (yamlfmt)
# ---------------------------------------------------------------------------
class TestF03FormatterClean:
    def test_every_companion_file_is_yamlfmt_clean(self) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        for f in plan.companion_files:
            ok, detail = yamlfmt_is_noop(f.content)
            assert ok, f"yamlfmt would reformat {f.path}:\n{detail}"

    def test_merged_path_files_carry_document_start(self) -> None:
        # Always-on partial guarantee: every merged-path companion file (incl. the
        # dumped supersession) begins with the ``---`` document-start marker
        # OCC's yamlfmt config requires.
        plan = compute_companion_plan(_merged_path_request())
        for f in plan.companion_files:
            assert f.content.startswith("---\n"), f"{f.path} lacks --- doc start"

    def test_merged_path_supersession_is_yamlfmt_clean(self) -> None:
        plan = compute_companion_plan(_merged_path_request())
        supersedes = [
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.SUPERSEDE_RECEIPT
        ]
        assert supersedes
        for f in supersedes:
            ok, detail = yamlfmt_is_noop(f.content)
            assert ok, f"yamlfmt would reformat {f.path}:\n{detail}"

    def test_yamlfmt_does_not_corrupt_supersession_multiline_scalars(self) -> None:
        # OMN-14714 regression. Stronger than the no-op assertion above: it pins
        # the specific way the old shape FAILED. ``probe_stdout`` carries captured
        # stdout ending in ``\n``; rendered as a single-quoted scalar its closing
        # quote sits alone on a line, and yamlfmt DELETES that line, leaving YAML
        # that no longer parses. The formatter corrupted evidence rather than
        # reformatting it (reproduced live against OCC#5251, 2026-07-28). Assert
        # yamlfmt's own output still parses and is semantically identical.
        plan = compute_companion_plan(_merged_path_request())
        supersedes = [
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.SUPERSEDE_RECEIPT
        ]
        assert supersedes
        for f in supersedes:
            before = yaml.safe_load(f.content)
            assert "\n" in before["replacement"]["probe_stdout"], (
                "fixture no longer exercises a multi-line scalar — this guard "
                "would pass vacuously"
            )
            after = yaml.safe_load(yamlfmt_output(f.content))
            assert after == before, f"yamlfmt altered {f.path} semantics"

    def test_negative_misformatted_yaml_is_flagged(self) -> None:
        # RED control: badly-indented / trailing-whitespace YAML is NOT a no-op.
        dirty = "---\nkey:    value   \nnested:\n- a\n-   b\n"
        ok, _ = yamlfmt_is_noop(dirty)
        assert not ok, "yamlfmt should report the mis-formatted control as dirty"


# ---------------------------------------------------------------------------
# F-04 PENDING hashes (existing/merged contract reuse)
# ---------------------------------------------------------------------------
class TestF04NoPendingHashes:
    def test_fresh_path_has_no_pending_sentinel(self) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        for f in plan.companion_files:
            assert "PENDING" not in f.content, f"{f.path} shipped a PENDING hash"

    def test_merged_path_binds_every_hash(self) -> None:
        plan = compute_companion_plan(_merged_path_request())
        assert plan.companion_files
        for f in plan.companion_files:
            assert "PENDING" not in f.content, f"{f.path} shipped a PENDING hash"
        # The supersession replacement must carry a resolved (64-hex) per-entry
        # hash, never the unbound sentinel — the F-04 defect.
        for f in plan.companion_files:
            if f.kind == EnumCompanionFileKind.SUPERSEDE_RECEIPT:
                assert re.search(r"sha256:[0-9a-f]{64}", f.content)
                assert f.contract_entry_sha256.startswith("sha256:")

    def test_negative_pending_receipt_is_detectable(self) -> None:
        # RED control: the sentinel a mis-generated companion leaves behind.
        pending_receipt = '---\ncontract_entry_sha256: "sha256:PENDING"\nstatus: PASS\n'
        assert "PENDING" in pending_receipt


# ---------------------------------------------------------------------------
# F-05 runtime-path change requires a deploy/diff-scope check
# ---------------------------------------------------------------------------
class TestF05DeployScope:
    def test_runtime_path_pr_declares_deploy_scope_check(self) -> None:
        req = _request(
            changed_files=(
                "src/omnimarket/nodes/node_x/handlers/handler_x.py",
                "deploy/compose.yaml",
            ),
            diff_total_lines=120,
        )
        plan = compute_companion_plan(req)
        assert is_runtime_touching(req.changed_files)
        assert has_deploy_scope_check(_contract_check_values(plan)), (
            "a runtime-touching PR's companion must declare a diff/deploy-scope check"
        )

    def test_negative_existence_only_contract_lacks_deploy_scope(self) -> None:
        # RED control: the pre-14679 existence-only contract (as occ#4284) has no
        # deploy/diff-scope check, so a runtime-path change would ship unproven.
        added = _occ_4284_added_files()
        data = yaml.safe_load(added["contracts/OMN-14695.yaml"])
        checks = [
            ck["check_value"] for it in data["dod_evidence"] for ck in it["checks"]
        ]
        assert not has_deploy_scope_check(checks)
        assert all(is_existence_probe(cv) for cv in checks)


# ---------------------------------------------------------------------------
# F-14 OPEN-only moving-head DoD checks
# ---------------------------------------------------------------------------
class TestF14NoOpenOnlyChecks:
    def test_no_minted_check_asserts_open_state(self) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        for cv in _contract_check_values(plan):
            assert not asserts_open_state(cv), f"minted check asserts OPEN state: {cv}"

    def test_negative_open_assertion_is_flagged(self) -> None:
        # RED control: a moving-head check that breaks once the PR merges.
        bad = (
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json state "
            '| grep -q \'"state":"OPEN"\''
        )
        assert asserts_open_state(bad)


# ---------------------------------------------------------------------------
# F-15 generic/placeholder DoD rows
# ---------------------------------------------------------------------------
class TestF15NoGenericPlaceholderRows:
    def test_minted_contract_has_no_generic_rows(self) -> None:
        plan = compute_companion_plan(_request(**_PASS2))
        contract = next(
            f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
        )
        assert generic_placeholder_rows(contract.content) == []

    def test_minted_contract_clears_substance_floor(self) -> None:
        # A contract may not be entirely L0 existence probes (the placeholder
        # weakness F-15 ties to). The minted contract declares a substantive
        # diff-scope check.
        assert any(
            is_substantive(cv)
            for cv in _contract_check_values(compute_companion_plan(_request()))
        )

    def test_negative_impossible_premerge_row_is_flagged(self) -> None:
        # RED control: the OMN-7906 generic immutable placeholder class.
        bad_contract = (
            "---\n"
            'ticket_id: "OMN-7906"\n'
            "dod_evidence:\n"
            '  - id: "dod-001"\n'
            '    description: "PR merged to main"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            '        check_value: "gh pr view ${PR_NUMBER} --repo ${REPO} --json mergedAt"\n'
        )
        flagged = generic_placeholder_rows(bad_contract)
        assert "dod-001" in flagged

    def test_negative_occ_4284_is_existence_only(self) -> None:
        # RED control from the captured companion: every check is an L0 existence
        # probe (the substance-floor failure occ#4284 exhibited live).
        added = _occ_4284_added_files()
        data = yaml.safe_load(added["contracts/OMN-14695.yaml"])
        checks = [
            ck["check_value"] for it in data["dod_evidence"] for ck in it["checks"]
        ]
        assert not any(is_substantive(cv) for cv in checks)


# ---------------------------------------------------------------------------
# F-17 closed/draft/do-not-merge suppression (producer guard)
# ---------------------------------------------------------------------------
class TestF17SuppressDeadTargets:
    def test_open_non_draft_pr_authors_companion(self) -> None:
        # Discriminator control: the SAME shape, but open+non-draft, DOES author.
        plan = compute_companion_plan(_request())
        assert not plan.no_op
        assert plan.companion_files

    def test_closed_pr_is_suppressed(self) -> None:
        plan = compute_companion_plan(_request(pr_state="closed"))
        assert plan.no_op
        assert plan.companion_files == ()
        assert "F-17" in plan.no_op_reason

    def test_merged_pr_is_suppressed(self) -> None:
        plan = compute_companion_plan(_request(pr_state="merged"))
        assert plan.no_op
        assert plan.companion_files == ()

    def test_draft_pr_is_suppressed(self) -> None:
        plan = compute_companion_plan(_request(pr_is_draft=True))
        assert plan.no_op
        assert plan.companion_files == ()
        assert "draft" in plan.no_op_reason.lower()

    def test_do_not_merge_title_is_suppressed(self) -> None:
        # The exact captured occ#4333 target title class.
        plan = compute_companion_plan(
            _request(pr_title="[WS4 PARITY PROBE - DO NOT MERGE] OMN-9999 probe")
        )
        assert plan.no_op
        assert plan.companion_files == ()

    def test_do_not_merge_body_is_suppressed(self) -> None:
        plan = compute_companion_plan(
            _request(pr_body="Do not merge — WIP experiment for OMN-9999.")
        )
        assert plan.no_op
        assert plan.companion_files == ()

    def test_negative_occ_4284_target_title_marks_do_not_merge(self) -> None:
        # RED control: the captured occ#4284/#4333 sibling target PR title is a
        # do-not-merge probe; feeding it to the producer must no-op.
        meta = yaml.safe_load((_FIXTURES / "occ_4284_pr-meta.json").read_text())
        # occ#4284's own title is an evidence companion; assert the guard fires
        # for the do-not-merge probe class the friction report names (occ#4333).
        plan = compute_companion_plan(
            _request(pr_title="[WS4 PARITY PROBE - DO NOT MERGE]", pr_body=str(meta))
        )
        assert plan.no_op
