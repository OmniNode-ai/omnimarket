# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""DurableEvidenceGate — pre-Linear-Done verification of the durable evidence trail.

The OMN-9855 incident (2026-04-30) closed a Linear ticket as Done after:

1. Probing that the implementation was already on ``omnibase_core/main``
   (PR #949 merged 2026-04-27).
2. Generating a DoD receipt LOCALLY, never committing it.
3. Updating the Linear ``dod_evidence`` description text to point at PR #949.

Result: Linear was green but the OCC governance ref still had the OLD contract
pointing at the superseded PR #926. The durable evidence trail was broken —
Linear-Done state was performative and unverifiable from origin alone.

Platform layout (OMN-12593, config-drift fix)
---------------------------------------------
The gate's default invocation must resolve against the *real* control-plane
layout, not a speculative one:

* The receipt is NOT a single ``evidence/<TICKET>/dod_report.json`` file. The
  platform (``node_pr_lifecycle_fix_effect`` / ``OccContractAdapter``) writes
  one receipt per evidence item at
  ``drift/dod_receipts/<TICKET>/<EVIDENCE_ITEM>/command.yaml``.
* OCC governance is dev-targeted: contracts and receipts land on the OCC ``dev``
  branch first and are batched to ``main`` later. The gate's default governance
  ref is therefore ``origin/dev`` — checking ``main`` falsely FAILs tickets
  whose evidence is genuinely durable on ``dev``.

Use :func:`default_receipt_dir` and :func:`default_contract_path` (and the
:data:`DEFAULT_OCC_GOVERNANCE_REF` constant) to obtain the canonical defaults,
or call :meth:`DurableEvidenceGate.evaluate_default` /
:meth:`DurableEvidenceGate.enforce_default` to run the gate against them
without restating the layout at each call site.

This service refuses the Linear Done transition when any of the following holds:

1. No receipt is tracked under ``drift/dod_receipts/<TICKET>/`` on the OCC
   governance ref (untracked or local-only commit).
2. The durable receipts under ``drift/dod_receipts/<TICKET>/`` do not bind at
   least one PASS ``pr_number`` + ``commit_sha`` receipt to a GitHub repository,
   or the bound PR is not ``MERGED`` with that ``mergeCommit.oid``.
3. The contract version on the OCC governance ref does not yet declare the
   receipt-bound evidence checks (i.e. the ref still has the stale contract).

The gate is pure logic plus pluggable Protocol probes (git receipt-tracked,
gh pr view, contract loader). Tests inject deterministic probe stubs;
production wiring uses subprocess implementations.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDefectLabel,
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    ModelCitedMergeCommit,
    ModelDurableEvidenceCheckResult,
    ModelDurableEvidenceGateResult,
)

_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>\d+)"
)

# Git short SHA is 7 hex chars; full is 40. Anything outside [7,40] hex chars
# is treated as a malformed citation and skipped (see CR thread on PR #467).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Canonical OCC governance ref. OCC governance is dev-targeted: contracts and
# receipts land on the OCC ``dev`` branch first and are batched to ``main``
# later (OMN-12593). Defaulting to ``main`` falsely FAILs tickets whose evidence
# is genuinely durable on ``dev``. ``origin/dev`` is the remote-tracking form so
# the probe verifies the pushed state, not a possibly-stale local ``dev``.
DEFAULT_OCC_GOVERNANCE_REF = "origin/dev"

# Canonical receipt directory prefix relative to the OCC repo root. The platform
# writes one receipt per evidence item at
# ``drift/dod_receipts/<TICKET>/<EVIDENCE_ITEM>/command.yaml`` — NOT a single
# ``evidence/<TICKET>/dod_report.json`` file (OMN-12593 config-drift fix).
_RECEIPT_DIR_PREFIX = "drift/dod_receipts"

# Canonical contract path prefix relative to the OCC repo root.
_CONTRACT_DIR_PREFIX = "contracts"


def default_receipt_dir(ticket_id: str) -> str:
    """Return the canonical OCC receipt directory for ``ticket_id``.

    The platform writes one receipt per evidence item under this directory at
    ``<dir>/<EVIDENCE_ITEM>/command.yaml``. The gate's receipt-tracked check
    asks whether *any* receipt is tracked under this directory on the OCC
    governance ref. Pure function — no I/O.
    """
    return f"{_RECEIPT_DIR_PREFIX}/{ticket_id}"


def default_contract_path(ticket_id: str) -> str:
    """Return the canonical OCC contract path for ``ticket_id``.

    Pure function — no I/O.
    """
    return f"{_CONTRACT_DIR_PREFIX}/{ticket_id}.yaml"


class DurableEvidenceGateError(Exception):
    """Raised when the durable-evidence gate refuses a Linear Done transition.

    Attributes:
        result: The structured ModelDurableEvidenceGateResult with each check's
            outcome. The first failed check identifies the blocking surface and
            the ``message`` field carries the remediation hint.
    """

    def __init__(self, result: ModelDurableEvidenceGateResult) -> None:
        self.result = result
        first_failure = next((c for c in result.checks if not c.passed), None)
        if first_failure is None:
            super().__init__(
                f"Durable-evidence gate failed for {result.ticket_id} "
                "(no per-check failure recorded)"
            )
        else:
            super().__init__(
                f"Durable-evidence gate failed for {result.ticket_id}: "
                f"{first_failure.check.value}: {first_failure.message}"
            )


class GitReceiptTrackedProbe(Protocol):
    """Probe: is any receipt tracked under ``receipt_dir`` on ``ref``?

    The platform writes one receipt per evidence item at
    ``<receipt_dir>/<EVIDENCE_ITEM>/command.yaml`` — there is no single fixed
    receipt filename. The probe answers whether the OCC governance ref tracks
    *at least one* receipt under the ticket's receipt directory. Production
    wiring runs ``git ls-tree -r --name-only <ref> -- <receipt_dir>`` and
    returns ``True`` when the listing is non-empty.
    """

    def __call__(self, repo_path: str, ref: str, receipt_dir: str) -> bool: ...


class GhPrViewProbe(Protocol):
    """Probe that returns ``(state, merge_commit_oid)`` for ``<owner>/<repo>#<num>``.

    ``state`` is the GitHub PR state (``"MERGED"``, ``"CLOSED"``, ``"OPEN"``).
    ``merge_commit_oid`` is the SHA of the merge commit, or ``None`` when the
    PR is not merged.
    """

    def __call__(self, repo: str, pr_number: int) -> tuple[str, str | None]: ...


class ContractOnRefLoader(Protocol):
    """Probe that returns the parsed contract YAML at ``<repo>:<ref>:<rel_path>``.

    Returns ``None`` when the contract does not exist on that ref.
    """

    def __call__(
        self, repo_path: str, ref: str, rel_path: str
    ) -> dict[str, object] | None: ...


class ReceiptsOnRefLoader(Protocol):
    """Probe that returns parsed receipt YAML payloads tracked under a receipt dir.

    Production wiring should load every ``*.yaml`` receipt under
    ``<repo>:<ref>:<receipt_dir>/``. The gate treats these receipts as the
    schema-valid source of PR/merge-commit bindings.
    """

    def __call__(
        self, repo_path: str, ref: str, receipt_dir: str
    ) -> list[dict[str, object]]: ...


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse ``https://github.com/<owner>/<repo>/pull/<n>`` into ``(repo, n)``.

    Returns ``None`` for unrecognized formats. Pure function — no I/O.
    """
    match = _PR_URL_RE.match(pr_url)
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("num"))


def extract_contract_check_keys(contract: dict[str, object]) -> set[tuple[str, str]]:
    """Extract schema-valid ``(evidence_item_id, check_type)`` keys from a contract."""
    keys: set[tuple[str, str]] = set()
    items = contract.get("dod_evidence", [])
    if not isinstance(items, list):
        return keys
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        checks = item.get("checks", [])
        if not isinstance(item_id, str) or not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            check_type = check.get("check_type")
            if isinstance(check_type, str):
                keys.add((item_id, check_type))
    return keys


def extract_defect_prevention(
    contract: dict[str, object],
) -> tuple[str | None, str | None]:
    """Extract the repair-to-ratchet fields from a defect contract (OMN-13339).

    Returns ``(prevention_gate, non_recurrence_note)`` where each is the
    stripped non-empty string value or ``None`` when absent/blank.

    ``prevention_gate`` links a CI workflow / pre-commit hook path or a PR that
    prevents recurrence of the defect class. ``non_recurrence_note`` is a
    structured explanation of why no automated gate is feasible. A defect
    ticket satisfies the rule when at least one is present.
    """

    def _nonblank(key: str) -> str | None:
        value = contract.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return _nonblank("prevention_gate"), _nonblank("non_recurrence_note")


def extract_receipt_merge_commits(
    receipts: list[dict[str, object]],
) -> list[ModelCitedMergeCommit]:
    """Extract PR/merge-commit citations from schema-valid receipt fields.

    ``ModelTicketContract`` forbids ad hoc ``pr_url``/``commit_sha`` fields on
    ``dod_evidence`` items and checks. The durable binding lives in
    ``ModelDodReceipt`` payloads: ``pr_number``, ``commit_sha``, and the probed
    GitHub repository encoded in ``probe_stdout``/``probe_command``/``check_value``.
    Receipts that do not carry a complete PASS PR binding are ignored; the gate
    fails when the complete receipt set yields zero citations.

    Pure function — no I/O.
    """
    citations: list[ModelCitedMergeCommit] = []
    seen: set[tuple[str, int, str]] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        if receipt.get("status") != "PASS":
            continue
        evidence_item_id = receipt.get("evidence_item_id")
        check_type = receipt.get("check_type")
        pr_number = receipt.get("pr_number")
        sha = receipt.get("commit_sha")
        if (
            not isinstance(evidence_item_id, str)
            or not isinstance(check_type, str)
            or not isinstance(pr_number, int)
            or not isinstance(sha, str)
            or not _SHA_RE.match(sha)
        ):
            continue
        repo = _extract_receipt_repo(receipt, pr_number)
        if repo is None:
            continue
        key = (repo, pr_number, sha)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            ModelCitedMergeCommit(
                pr_url=f"https://github.com/{repo}/pull/{pr_number}",
                repo=repo,
                pr_number=pr_number,
                cited_sha=sha,
                evidence_item_id=evidence_item_id,
                check_type=check_type,
            )
        )
    return citations


def _extract_receipt_repo(receipt: dict[str, object], pr_number: int) -> str | None:
    """Resolve ``owner/repo`` for a PR-bound receipt from schema fields."""
    fields = ("probe_stdout", "probe_command", "check_value")
    for field in fields:
        value = receipt.get(field)
        if not isinstance(value, str):
            continue
        repo = _extract_repo_from_github_json(value, pr_number)
        if repo is not None:
            return repo
        repo = _extract_repo_from_text(value, pr_number)
        if repo is not None:
            return repo
    return None


def _extract_repo_from_github_json(value: str, pr_number: int) -> str | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    number = parsed.get("number")
    if number != pr_number:
        return None
    url = parsed.get("url")
    if not isinstance(url, str):
        return None
    parsed_url = parse_pr_url(url)
    if parsed_url is None:
        return None
    repo, number_from_url = parsed_url
    if number_from_url != pr_number:
        return None
    return repo


def _extract_repo_from_text(value: str, pr_number: int) -> str | None:
    repo_match = re.search(
        r"(?:^|\s)--repo(?:=|\s+)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
        value,
    )
    if repo_match is not None:
        return repo_match.group("repo")
    for match in re.finditer(
        r"https://github\.com/[^\s\"')]+/[^\s\"')]+/pull/\d+(?:/[^\s\"')]*)?",
        value,
    ):
        parsed = parse_pr_url(match.group(0))
        if parsed is None:
            continue
        repo, number = parsed
        if number == pr_number:
            return repo
    return None


class DurableEvidenceGate:
    """Pure-logic gate that refuses Linear Done if durable evidence is local-only.

    Construction takes three Protocol-typed probes so unit tests can inject
    deterministic stubs. Production wiring uses subprocess-backed
    implementations.

    The OCC governance ref defaults to :data:`DEFAULT_OCC_GOVERNANCE_REF`
    (``origin/dev``) because OCC governance is dev-targeted — contracts and
    receipts land on ``dev`` first and are batched to ``main`` later. Probing
    ``main`` falsely FAILs tickets whose evidence is genuinely durable on
    ``dev`` (OMN-12593).
    """

    def __init__(
        self,
        *,
        is_receipt_tracked: GitReceiptTrackedProbe,
        gh_pr_view: GhPrViewProbe,
        load_contract_on_ref: ContractOnRefLoader,
        load_receipts_on_ref: ReceiptsOnRefLoader,
        occ_repo_path: str,
        occ_governance_ref: str = DEFAULT_OCC_GOVERNANCE_REF,
    ) -> None:
        self._is_receipt_tracked = is_receipt_tracked
        self._gh_pr_view = gh_pr_view
        self._load_contract_on_ref = load_contract_on_ref
        self._load_receipts_on_ref = load_receipts_on_ref
        self._occ_repo_path = occ_repo_path
        self._occ_governance_ref = occ_governance_ref

    def evaluate_default(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str] = frozenset(),
    ) -> ModelDurableEvidenceGateResult:
        """Run the gate against the canonical platform layout for ``ticket_id``.

        This is the DEFAULT invocation. It resolves the receipt directory and
        contract path from the canonical platform layout
        (:func:`default_receipt_dir` / :func:`default_contract_path`) so callers
        cannot accidentally pass a drifted ``evidence/<TICKET>/dod_report.json``
        path or check the wrong governance ref. Equivalent to::

            gate.evaluate(
                ticket_id=ticket_id,
                contract=contract,
                receipt_dir=default_receipt_dir(ticket_id),
                contract_rel_path=default_contract_path(ticket_id),
            )

        Pure result — does not raise. Callers that want hard-fail semantics
        invoke :meth:`enforce_default` instead.
        """
        return self.evaluate(
            ticket_id=ticket_id,
            contract=contract,
            receipt_dir=default_receipt_dir(ticket_id),
            contract_rel_path=default_contract_path(ticket_id),
            ticket_labels=ticket_labels,
        )

    def evaluate(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        receipt_dir: str,
        contract_rel_path: str,
        ticket_labels: frozenset[str] = frozenset(),
    ) -> ModelDurableEvidenceGateResult:
        """Run the three durable-evidence checks and return an aggregate result.

        Args:
            ticket_id: The Linear ticket ID (e.g. ``OMN-9855``).
            contract: The parsed local contract dict — already validated by the
                EvidenceCollector load path.
            receipt_dir: The OCC-root-relative receipt directory for the ticket,
                e.g. ``drift/dod_receipts/OMN-12574``. The check passes when at
                least one receipt is tracked under this directory on the OCC
                governance ref. Use :func:`default_receipt_dir` to build it.
            contract_rel_path: Path to the contract YAML relative to the OCC
                repo root, e.g. ``contracts/OMN-9855.yaml``. Use
                :func:`default_contract_path` to build it.

        Pure result — does not raise. Callers that want hard-fail semantics
        invoke :meth:`enforce` instead.
        """
        checks: list[ModelDurableEvidenceCheckResult] = []

        # Check 1: at least one receipt is tracked under the ticket's receipt
        # directory on the OCC governance ref.
        receipt_tracked = self._is_receipt_tracked(
            self._occ_repo_path, self._occ_governance_ref, receipt_dir
        )
        if receipt_tracked:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.RECEIPT_TRACKED,
                    passed=True,
                    message=(
                        f"Receipt(s) under {receipt_dir}/ are tracked on "
                        f"{self._occ_governance_ref}."
                    ),
                )
            )
        else:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.RECEIPT_TRACKED,
                    passed=False,
                    message=(
                        f"No receipt is tracked under {receipt_dir}/ on "
                        f"{self._occ_governance_ref}. Commit and push the "
                        f"command.yaml receipt to onex_change_control before "
                        f"re-running the gate."
                    ),
                )
            )

        receipts = self._load_receipts_on_ref(
            self._occ_repo_path, self._occ_governance_ref, receipt_dir
        )

        # Check 2: every PR-bound receipt is MERGED with mergeCommit.oid == commit_sha.
        citations = extract_receipt_merge_commits(receipts)
        check2_failure: str | None = None
        if not citations:
            check2_failure = (
                f"No PASS receipt under {receipt_dir}/ binds pr_number, "
                "commit_sha, and a GitHub repo. Durable evidence must cite an "
                "actual merged PR via receipt fields before Linear can move to Done."
            )
        else:
            for citation in citations:
                state, merge_commit_oid = self._gh_pr_view(
                    citation.repo, citation.pr_number
                )
                if state != "MERGED":
                    check2_failure = (
                        f"{citation.pr_url} state={state}, expected MERGED. "
                        "Update the durable receipt to cite the real merged PR "
                        "before re-running the gate."
                    )
                    break
                if merge_commit_oid is None or (
                    not merge_commit_oid.startswith(citation.cited_sha[:7])
                    and not citation.cited_sha.startswith(merge_commit_oid[:7])
                ):
                    check2_failure = (
                        f"{citation.pr_url} mergeCommit.oid="
                        f"{merge_commit_oid!r} does not match receipt "
                        f"commit_sha={citation.cited_sha!r}. The durable receipt "
                        "is citing a superseded, head-only, or wrong commit — "
                        "update the receipt to the actual merge commit before "
                        "re-running the gate."
                    )
                    break
        if check2_failure is None:
            cite_msg = (
                f"All {len(citations)} receipt-bound PR(s) are MERGED with "
                "matching merge SHAs."
            )
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
                    passed=True,
                    message=cite_msg,
                )
            )
        else:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
                    passed=False,
                    message=check2_failure,
                )
            )

        # Check 3: the OCC governance ref contains a contract version declaring
        # the schema-valid evidence checks for the receipt-bound PR commits.
        # This catches stale refs where the receipt exists but the contract on
        # the governance ref does not yet declare the bound evidence item.
        main_contract = self._load_contract_on_ref(
            self._occ_repo_path, self._occ_governance_ref, contract_rel_path
        )
        if main_contract is None:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                    passed=False,
                    message=(
                        f"Contract {contract_rel_path} is not present on "
                        f"{self._occ_governance_ref}. Open an OCC PR with the "
                        "contract and merge it before transitioning Linear to "
                        "Done."
                    ),
                )
            )
        else:
            local_contract_keys = extract_contract_check_keys(contract)
            main_contract_keys = extract_contract_check_keys(main_contract)
            receipt_keys = {(c.evidence_item_id, c.check_type) for c in citations}
            missing_contract_keys = local_contract_keys - main_contract_keys
            missing_receipt_keys = receipt_keys - main_contract_keys
            if missing_contract_keys or missing_receipt_keys:
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                        passed=False,
                        message=(
                            f"Contract on {self._occ_governance_ref} is stale — "
                            f"missing local check(s) {sorted(missing_contract_keys)} "
                            f"or receipt-bound check(s) {sorted(missing_receipt_keys)}. "
                            "Open an OCC PR to update the contract and merge it "
                            "before transitioning Linear to Done."
                        ),
                    )
                )
            else:
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                        passed=True,
                        message=(
                            f"Contract on {self._occ_governance_ref} contains "
                            "the receipt-bound evidence checks."
                        ),
                    )
                )

        # Check 4 (OMN-13339, retro R4 — repair-to-ratchet): a defect-labelled
        # ticket cannot close without a linked prevention gate (CI workflow /
        # pre-commit hook path or PR) OR a structured non-recurrence note. This
        # converts repairs into ratchets per Rule 5 so the same failure class
        # does not return. Non-defect tickets are exempt (the check passes N/A).
        defect_labels = EnumDefectLabel.values()
        present_defect_labels = sorted(ticket_labels & defect_labels)
        if not present_defect_labels:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
                    passed=True,
                    message=(
                        "Not a defect-class ticket — repair-to-ratchet rule does "
                        "not apply."
                    ),
                )
            )
        else:
            prevention_gate, non_recurrence_note = extract_defect_prevention(contract)
            if prevention_gate is not None or non_recurrence_note is not None:
                satisfied_by = (
                    f"prevention_gate={prevention_gate!r}"
                    if prevention_gate is not None
                    else f"non_recurrence_note={non_recurrence_note!r}"
                )
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
                        passed=True,
                        message=(
                            f"Defect ticket (label(s) {present_defect_labels}) "
                            f"satisfies repair-to-ratchet via {satisfied_by}."
                        ),
                    )
                )
            else:
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
                        passed=False,
                        message=(
                            f"Defect ticket (label(s) {present_defect_labels}) "
                            "cannot close: the contract links no prevention gate "
                            "and carries no non-recurrence note. Add a top-level "
                            "'prevention_gate' (CI workflow / pre-commit hook path "
                            "or PR URL) OR a 'non_recurrence_note' explaining why "
                            "no automated gate is feasible before transitioning "
                            "Linear to Done (OMN-13339, Rule 5)."
                        ),
                    )
                )

        # Check 4 (OMN-13337, retro R2): the ticket must carry at least one
        # approved done-class label, and the label must be backed by durable
        # evidence. A plain-Done ticket with no class label — or a labelled
        # ticket whose receipt is not tracked on the governance ref — is
        # rejected here so done-detection cannot be gamed with a bare Done.
        approved_labels = EnumDoneClassLabel.values()
        present_done_classes = sorted(ticket_labels & approved_labels)
        if not present_done_classes:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
                    passed=False,
                    message=(
                        "Ticket carries no approved done-class label. Add exactly "
                        "one of "
                        f"{sorted(approved_labels)} that reflects how the work was "
                        "proven Done (backed by RECEIPT_TRACKED / "
                        "CONTRACT_CITES_MERGE_COMMIT / CONTRACT_ON_OCC_MAIN) "
                        "before transitioning Linear to Done. A plain Done with no "
                        "done-class label is rejected (OMN-13337)."
                    ),
                )
            )
        elif not receipt_tracked:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
                    passed=False,
                    message=(
                        f"Ticket carries done-class label(s) {present_done_classes} "
                        "but no durable receipt is tracked on "
                        f"{self._occ_governance_ref}. A done-class label must be "
                        "backed by a tracked receipt (RECEIPT_TRACKED) — the label "
                        "alone is not evidence (OMN-13337)."
                    ),
                )
            )
        else:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
                    passed=True,
                    message=(
                        f"Done-class label(s) {present_done_classes} present and "
                        "backed by a tracked durable receipt."
                    ),
                )
            )

        all_pass = all(c.passed for c in checks)
        return ModelDurableEvidenceGateResult(
            ticket_id=ticket_id,
            status=(
                EnumDurableEvidenceStatus.PASS
                if all_pass
                else EnumDurableEvidenceStatus.FAIL
            ),
            checks=checks,
        )

    def enforce(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        receipt_dir: str,
        contract_rel_path: str,
        ticket_labels: frozenset[str] = frozenset(),
    ) -> ModelDurableEvidenceGateResult:
        """Run :meth:`evaluate` and raise on failure.

        On failure raises :class:`DurableEvidenceGateError` carrying the
        structured result. On success returns the result.
        """
        result = self.evaluate(
            ticket_id=ticket_id,
            contract=contract,
            receipt_dir=receipt_dir,
            contract_rel_path=contract_rel_path,
            ticket_labels=ticket_labels,
        )
        if result.status != EnumDurableEvidenceStatus.PASS:
            raise DurableEvidenceGateError(result)
        return result

    def enforce_default(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str] = frozenset(),
    ) -> ModelDurableEvidenceGateResult:
        """Run :meth:`evaluate_default` and raise on failure.

        This is the DEFAULT hard-fail invocation: it resolves the canonical
        platform paths internally. On failure raises
        :class:`DurableEvidenceGateError` carrying the structured result. On
        success returns the result.
        """
        result = self.evaluate_default(
            ticket_id=ticket_id,
            contract=contract,
            ticket_labels=ticket_labels,
        )
        if result.status != EnumDurableEvidenceStatus.PASS:
            raise DurableEvidenceGateError(result)
        return result


__all__: list[str] = [
    "DEFAULT_OCC_GOVERNANCE_REF",
    "ContractOnRefLoader",
    "DurableEvidenceGate",
    "DurableEvidenceGateError",
    "GhPrViewProbe",
    "GitReceiptTrackedProbe",
    "ReceiptsOnRefLoader",
    "default_contract_path",
    "default_receipt_dir",
    "extract_contract_check_keys",
    "extract_receipt_merge_commits",
    "parse_pr_url",
]
