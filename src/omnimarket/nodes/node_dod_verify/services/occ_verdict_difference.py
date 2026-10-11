# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare same-head receipt-gate and OCC verdicts for S5 (OMN-20072).

The caller supplies verdict artifacts for the same PR head. An expected
difference is enforced as the stricter path's verdict: earlier caller steps
already enforce the new path's own refusal, and OCC's context stays required.
This check decides only whether the difference itself is allowed.

OMN-20074, operator RULING 2026-10-08T14:59:17Z extends RULING
2026-10-08T09:57:41Z: contract_in_another_repo (expected difference) also covers
a PR whose ticket has its only other contract in onex_change_control. A missing
head whose caller-supplied contract-home marker names any repository, including
onex_change_control, refuses with ``contract_in_another_repo``.

A PR labelled as a negative control is meant to be refused. Its refusal passes
whatever OCC said, because every negative control must be rejected by the new
path even when OCC accepted it; its admission fails as
``accepted_negative_control``.

OMN-20917: a PR whose product side is only dependency pins and lock files, refused by
the new path's must-fail control (its bound test also passes at the merge
base) while OCC admitted, is the expected difference ``dependency_repin``; the
same refusal on a PR that also changes other product paths stays unclassified.
Its own contract and test side are the evidence the gate needs and do not
disqualify it.

Classification is pure; only the two loader helpers read supplied files.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.enums.enum_occ_verdict_difference_reason import (
    EnumOccVerdictDifferenceReason,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelNewPathVerdict,
    ModelOccVerdict,
    ModelOccVerdictDifferenceResult,
    OccDifferenceOutcome,
)
from omnimarket.occ_content_probe import classify_dependency_pin_only

# Published OR.1 inventory: reason_code -> (old_path, new_path, expected).
EXPECTED_DIFFERENCES: Final[Mapping[str, tuple[str, str, bool]]] = {
    "readback_only": ("may_admit", "refuse", True),
    "incomplete_criterion_coverage": ("may_admit", "refuse", True),
    "circular_contract": ("may_admit", "refuse", True),
    "final_newline": ("admits_after_receipt_hash_recompute", "refuse", True),
    "PR_number_only_binding": ("admits_stale_or_foreign_commit", "refuse", True),
    "contract_in_another_repo": ("may_admit", "refuse", True),
    "foreign_policy_outside_declared_manifest": ("may_refuse", "admit", True),
    "dependency_repin": ("may_admit", "refuse", True),
    "old_behavioral_refusal": ("refuse", "admit", False),
    "unclassified": ("any", "any", False),
    "accepted_negative_control": ("any", "admit", False),
}

# Plain EnumOccEligibilityReason wire strings keep parsing total for unknown
# values without coupling the verifier to omnibase_core's enum version.
OCC_BEHAVIOURAL_REASONS: Final[frozenset[str]] = frozenset(
    {
        "nonpass_receipt",
        "goal_attempt_nonpass",
        "goal_criterion_coverage_missing",
        "goal_criterion_baseline_mismatch",
    }
)
# OCC repository policy outside the product PR's declared manifest.
OCC_FOREIGN_POLICY_REASONS: Final[frozenset[str]] = frozenset({"occ_not_on_main"})

_NEW_PATH_STRICTER_REASONS: Final[frozenset[str]] = frozenset(
    reason
    for reason, (_, new_path, expected) in EXPECTED_DIFFERENCES.items()
    if new_path == "refuse" and expected
)
_OCC_REFUSAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"OCC PREFLIGHT FAILED: reason=([A-Za-z_]+)"
)

# OMN-20917: the lock files a dependency re-pin may touch beside the
# pyproject.toml and uv.lock that classify_dependency_pin_only already reads.
# This names a difference reason, never an exemption: the new path still
# refuses such a PR, so this set does not widen DEPENDENCY_LOCK_BASENAMES.
DEPENDENCY_REPIN_PACKAGE_LOCK_BASENAMES: Final[frozenset[str]] = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"}
)

# OMN-20917: receipt-gate.yml TEST_SIDE, overlaid on the merge base.
TEST_SIDE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^tests?/|/tests?/|(^|/)conftest\.py$|(^|/)test_[^/]*\.py$|_test\.py$"
)


def is_test_side(path: str) -> bool:
    return TEST_SIDE_PATTERN.search(path) is not None


def classify_dependency_repin(
    changed_paths: Sequence[str],
    *,
    tickets: Sequence[str] = (),
    pyproject_head: str | None,
    pyproject_base: str | None,
) -> tuple[bool, str]:
    """OMN-20917: is every product path a dependency pin or a lock file?

    Drop tests and cited contracts, the evidence overlaid on the merge base.
    A contract the title does not cite stays product side.
    pyproject.toml and uv.lock are judged by ``classify_dependency_pin_only``
    (only version and dependency-pin keys may differ); a package lock file
    may change freely. Any other path, an empty diff or an unreadable
    manifest is not a re-pin.
    """
    if not changed_paths:
        return False, "no changed files observed"
    cited = {f"contracts/{ticket}.yaml" for ticket in tickets}
    product_paths = [
        path for path in changed_paths if not is_test_side(path) and path not in cited
    ]
    if not product_paths:
        return False, "only the evidence side changed"
    python_paths = [
        path
        for path in product_paths
        if path.rsplit("/", 1)[-1] not in DEPENDENCY_REPIN_PACKAGE_LOCK_BASENAMES
    ]
    if not python_paths:
        return True, "package lock files only"
    return classify_dependency_pin_only(
        python_paths, pyproject_head=pyproject_head, pyproject_base=pyproject_base
    )


def parse_occ_verdict(check_run: object) -> ModelOccVerdict:
    """Parse a GitHub check-run object; unknown or absent conclusions are unavailable."""
    if not isinstance(check_run, dict):
        return ModelOccVerdict(admitted=None)
    conclusion = check_run.get("conclusion")
    if not isinstance(conclusion, str):
        return ModelOccVerdict(admitted=None)
    if conclusion == "success":
        return ModelOccVerdict(admitted=True, conclusion=conclusion)
    if conclusion != "failure":
        return ModelOccVerdict(admitted=None, conclusion=conclusion)

    annotations = check_run.get("annotations", [])
    if isinstance(annotations, list):
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            message = annotation.get("message")
            if isinstance(message, str):
                match = _OCC_REFUSAL_PATTERN.search(message)
                if match:
                    return ModelOccVerdict(
                        admitted=False, conclusion=conclusion, reason=match.group(1)
                    )
    return ModelOccVerdict(admitted=False, conclusion=conclusion)


def load_occ_verdict(check_run_path: Path) -> ModelOccVerdict:
    """Read OCC JSON; a missing, null, or malformed artifact is unavailable."""
    try:
        payload: object = json.loads(check_run_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    return parse_occ_verdict(payload)


def _ticket_verdict(
    head: object, control_first_line: str, dependency_repin: bool = False
) -> ModelNewPathVerdict:
    """Derive one ticket's verdict from an already-loaded head and base control."""
    if not isinstance(head, dict):
        return ModelNewPathVerdict(admitted=False)
    raw_checks = head.get("checks", [])
    checks = (
        [check for check in raw_checks if isinstance(check, dict)]
        if isinstance(raw_checks, list)
        else []
    )
    bound_checks = [check for check in checks if check.get("binds_ac")]
    if head.get("status") != "verified":
        error_message = head.get("error_message")
        # OMN-20070: the unbound-criterion refusal, never a failed check that
        # happens to sit beside an unbound criterion.
        unbound_criteria = head.get("acceptance_unbound_criteria")
        if (
            isinstance(unbound_criteria, list)
            and unbound_criteria
            and isinstance(error_message, str)
            and error_message.startswith("NO_ACCEPTANCE_CHECKS")
        ):
            return ModelNewPathVerdict(
                admitted=False,
                reason=EnumOccVerdictDifferenceReason.INCOMPLETE_CRITERION_COVERAGE.value,
            )
        if isinstance(error_message, str) and error_message.startswith(
            ("NO_ACCEPTANCE_CHECKS", "NO_PROBATIVE_EVIDENCE")
        ):
            return ModelNewPathVerdict(
                admitted=False,
                reason=EnumOccVerdictDifferenceReason.READBACK_ONLY.value,
            )
        failed_bound_checks = [
            check for check in bound_checks if check.get("status") == "failed"
        ]
        if failed_bound_checks and all(
            check.get("proof_class") == EnumCheckProofClass.MERGE_STATE.value
            for check in failed_bound_checks
        ):
            return ModelNewPathVerdict(
                admitted=False,
                reason=EnumOccVerdictDifferenceReason.CIRCULAR_CONTRACT.value,
            )
        return ModelNewPathVerdict(admitted=False)
    if not control_first_line.startswith("passed"):
        reason: str | None = None
        if not bound_checks:
            reason = EnumOccVerdictDifferenceReason.INCOMPLETE_CRITERION_COVERAGE.value
        elif dependency_repin:
            # OMN-20917: a re-pin's bound test also passes at the merge base.
            reason = EnumOccVerdictDifferenceReason.DEPENDENCY_REPIN.value
        return ModelNewPathVerdict(admitted=False, reason=reason)
    return ModelNewPathVerdict(admitted=True)


def _contract_home_repository(line: str) -> str:
    """Strip whitespace and an optional owner; return the bare repository name."""
    return line.strip().split("/", 1)[-1]


def load_new_verdict(
    dod_dir: Path, tickets: Sequence[str], *, dependency_repin: bool = False
) -> ModelNewPathVerdict:
    """Require every sorted ticket's verified head and passed base control.

    OMN-20074, ruling 2026-10-08T09:57:41Z: when head is None, the first
    non-empty line of contract-home-<ticket>.txt may name another repository
    holding contracts/<ticket>.yaml, yielding contract_in_another_repo. Operator
    RULING 2026-10-08T14:59:17Z extends RULING 2026-10-08T09:57:41Z:
    contract_in_another_repo (expected difference) also covers a PR whose ticket
    has its only other contract in onex_change_control. A marker naming
    onex_change_control also yields contract_in_another_repo. An absent, empty,
    or unreadable marker leaves the refusal unclassified.
    A present head ignores the marker. Missing or empty controls have no passed
    first line. An empty ticket list refuses without a reason.
    ``dependency_repin`` (OMN-20917, from ``classify_dependency_repin`` over the
    PR's diff) names a must-fail control refusal with bound checks
    ``dependency_repin``; it changes no other verdict.
    """
    if not tickets:
        return ModelNewPathVerdict(admitted=False)
    for ticket in sorted(tickets):
        try:
            head: object = json.loads(
                (dod_dir / f"head-{ticket}.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            head = None
        if head is None:
            try:
                contract_home_lines = (
                    (dod_dir / f"contract-home-{ticket}.txt")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
            except (OSError, UnicodeError):
                contract_home_lines = []
            contract_home = next(
                (line for line in contract_home_lines if line.strip()), ""
            )
            if _contract_home_repository(contract_home):
                return ModelNewPathVerdict(
                    admitted=False,
                    reason=EnumOccVerdictDifferenceReason.CONTRACT_IN_ANOTHER_REPO.value,
                )
        try:
            control_lines = (
                (dod_dir / f"base-{ticket}.control.txt")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        except (OSError, UnicodeError):
            control_lines = []
        verdict = _ticket_verdict(
            head, control_lines[0] if control_lines else "", dependency_repin
        )
        if not verdict.admitted:
            return verdict
    return ModelNewPathVerdict(admitted=True)


def classify(
    old: ModelOccVerdict, new: ModelNewPathVerdict, negative_control: bool
) -> ModelOccVerdictDifferenceResult:
    """Apply the ordered S5 rules, deciding whether the difference is allowed."""
    reason_code: EnumOccVerdictDifferenceReason | None = None
    outcome_name: OccDifferenceOutcome
    # Construct through one exit so every branch names both verdicts and reasons.
    if new.admitted and negative_control:
        passed = False
        outcome_name = "forbidden_difference"
        reason_code = EnumOccVerdictDifferenceReason.ACCEPTED_NEGATIVE_CONTROL
    elif negative_control:
        passed = True
        outcome_name = "negative_control_refused"
    elif old.admitted is None:
        passed = False
        outcome_name = "not_compared"
    elif old.admitted == new.admitted:
        passed = True
        outcome_name = "agree"
    elif old.admitted:
        passed = new.reason in _NEW_PATH_STRICTER_REASONS
        outcome_name = "expected_difference" if passed else "unclassified_difference"
        reason_code = (
            EnumOccVerdictDifferenceReason(new.reason)
            if passed and new.reason is not None
            else EnumOccVerdictDifferenceReason.UNCLASSIFIED
        )
    elif old.reason in OCC_BEHAVIOURAL_REASONS:
        passed = False
        outcome_name = "forbidden_difference"
        reason_code = EnumOccVerdictDifferenceReason.OLD_BEHAVIORAL_REFUSAL
    elif old.reason in OCC_FOREIGN_POLICY_REASONS:
        passed = True
        outcome_name = "expected_difference"
        reason_code = (
            EnumOccVerdictDifferenceReason.FOREIGN_POLICY_OUTSIDE_DECLARED_MANIFEST
        )
    else:
        passed = False
        outcome_name = "unclassified_difference"
        reason_code = EnumOccVerdictDifferenceReason.UNCLASSIFIED

    old_label = (
        f"OCC verdict unavailable (conclusion={json.dumps(old.conclusion)})"
        if old.admitted is None
        else f"OCC={'admitted' if old.admitted else 'refused'}"
    )
    code_label = f"reason_code={reason_code.value}" if reason_code else "no reason code"
    outcome = ModelOccVerdictDifferenceResult(
        passed=passed,
        outcome=outcome_name,
        reason_code=reason_code,
        old_admitted=old.admitted,
        new_admitted=new.admitted,
        old_reason=old.reason,
        new_reason=new.reason,
        message=(
            f"{old_label} (reason={json.dumps(old.reason)}); "
            f"new={'admitted' if new.admitted else 'refused'} "
            f"(reason={json.dumps(new.reason)}); {code_label}"
        ),
    )
    return outcome
