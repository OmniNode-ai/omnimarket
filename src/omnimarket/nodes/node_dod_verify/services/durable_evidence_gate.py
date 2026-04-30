# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""DurableEvidenceGate — pre-Linear-Done verification of the durable evidence trail.

The OMN-9855 incident (2026-04-30) closed a Linear ticket as Done after:

1. Probing that the implementation was already on ``omnibase_core/main``
   (PR #949 merged 2026-04-27).
2. Generating a DoD receipt at ``onex_change_control/evidence/OMN-9855/dod_report.json``
   LOCALLY, never committing it.
3. Updating the Linear ``dod_evidence`` description text to point at PR #949.

Result: Linear was green but ``onex_change_control/main`` still had the OLD
contract pointing at the superseded PR #926. The durable evidence trail was
broken — Linear-Done state was performative and unverifiable from origin alone.

This service refuses the Linear Done transition when any of the following holds:

1. The receipt at ``evidence/<TICKET>/dod_report.json`` is not tracked on
   ``onex_change_control/main`` (untracked or local-only commit).
2. The contract's ``dod_evidence`` cites a ``pr_url`` whose state is not
   ``MERGED`` or whose ``mergeCommit.oid`` does not match the cited SHA.
3. The contract version on ``onex_change_control/main`` does not yet contain
   the real merge commit citation (i.e. main still has the stale contract).

The gate is pure logic plus three pluggable Protocol probes (git ls-tree,
gh pr view, contract loader). Tests inject deterministic probe stubs;
production wiring uses subprocess implementations.
"""

from __future__ import annotations

import re
from typing import Protocol

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    ModelCitedMergeCommit,
    ModelDurableEvidenceCheckResult,
    ModelDurableEvidenceGateResult,
)

_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>\d+)"
)


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


class GitTrackedProbe(Protocol):
    """Probe that returns whether ``rel_path`` is tracked on a given git ref."""

    def __call__(self, repo_path: str, ref: str, rel_path: str) -> bool: ...


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


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse ``https://github.com/<owner>/<repo>/pull/<n>`` into ``(repo, n)``.

    Returns ``None`` for unrecognized formats. Pure function — no I/O.
    """
    match = _PR_URL_RE.match(pr_url)
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("num"))


def extract_cited_merge_commits(
    contract: dict[str, object],
) -> list[ModelCitedMergeCommit]:
    """Extract ``(pr_url, commit_sha)`` citations from a contract's dod_evidence.

    The contract schema's ``dod_evidence[]`` items may declare ``pr_url`` and
    ``commit_sha`` fields directly, or nest them inside ``checks[]`` entries.
    The gate inspects both shapes; missing or malformed citations are skipped
    rather than failing the gate (a contract with zero citations is fine —
    only contracts that DO cite must cite real merged commits).

    Pure function — no I/O.
    """
    citations: list[ModelCitedMergeCommit] = []
    seen: set[tuple[str, str]] = set()
    items = contract.get("dod_evidence", [])
    if not isinstance(items, list):
        return citations
    for item in items:
        if not isinstance(item, dict):
            continue
        candidates: list[dict[str, object]] = [item]
        nested = item.get("checks", [])
        if isinstance(nested, list):
            for c in nested:
                if isinstance(c, dict):
                    candidates.append(c)
        for cand in candidates:
            pr_url = cand.get("pr_url")
            sha = cand.get("commit_sha")
            if not isinstance(pr_url, str) or not isinstance(sha, str):
                continue
            parsed = parse_pr_url(pr_url)
            if parsed is None:
                continue
            key = (pr_url, sha)
            if key in seen:
                continue
            seen.add(key)
            repo, num = parsed
            citations.append(
                ModelCitedMergeCommit(
                    pr_url=pr_url,
                    repo=repo,
                    pr_number=num,
                    cited_sha=sha,
                )
            )
    return citations


class DurableEvidenceGate:
    """Pure-logic gate that refuses Linear Done if durable evidence is local-only.

    Construction takes three Protocol-typed probes so unit tests can inject
    deterministic stubs. Production wiring uses subprocess-backed
    implementations under ``services/durable_evidence_gate_probes.py``
    (out of scope for the first slice — this module ships the gate logic and
    error type only).
    """

    def __init__(
        self,
        *,
        is_tracked: GitTrackedProbe,
        gh_pr_view: GhPrViewProbe,
        load_contract_on_ref: ContractOnRefLoader,
        occ_repo_path: str,
        occ_main_ref: str = "main",
    ) -> None:
        self._is_tracked = is_tracked
        self._gh_pr_view = gh_pr_view
        self._load_contract_on_ref = load_contract_on_ref
        self._occ_repo_path = occ_repo_path
        self._occ_main_ref = occ_main_ref

    def evaluate(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        receipt_rel_path: str,
        contract_rel_path: str,
    ) -> ModelDurableEvidenceGateResult:
        """Run the three durable-evidence checks and return an aggregate result.

        Args:
            ticket_id: The Linear ticket ID (e.g. ``OMN-9855``).
            contract: The parsed local contract dict — already validated by the
                EvidenceCollector load path.
            receipt_rel_path: Path to the DoD receipt relative to the OCC repo
                root, e.g. ``evidence/OMN-9855/dod_report.json``.
            contract_rel_path: Path to the contract YAML relative to the OCC
                repo root, e.g. ``contracts/OMN-9855.yaml``.

        Pure result — does not raise. Callers that want hard-fail semantics
        invoke :meth:`enforce` instead.
        """
        checks: list[ModelDurableEvidenceCheckResult] = []

        # Check 1: receipt is tracked on OCC main
        receipt_tracked = self._is_tracked(
            self._occ_repo_path, self._occ_main_ref, receipt_rel_path
        )
        if receipt_tracked:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.RECEIPT_TRACKED,
                    passed=True,
                    message=(
                        f"Receipt {receipt_rel_path} is tracked on "
                        f"{self._occ_main_ref}."
                    ),
                )
            )
        else:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.RECEIPT_TRACKED,
                    passed=False,
                    message=(
                        f"Receipt {receipt_rel_path} is NOT tracked on "
                        f"{self._occ_main_ref}. Commit and push the receipt to "
                        f"onex_change_control before re-running the gate."
                    ),
                )
            )

        # Check 2: every cited PR is MERGED with mergeCommit.oid == cited_sha
        citations = extract_cited_merge_commits(contract)
        check2_failure: str | None = None
        for citation in citations:
            state, merge_commit_oid = self._gh_pr_view(
                citation.repo, citation.pr_number
            )
            if state != "MERGED":
                check2_failure = (
                    f"{citation.pr_url} state={state}, expected MERGED. "
                    "Update the contract dod_evidence to cite the real merged PR "
                    "before re-running the gate."
                )
                break
            if merge_commit_oid is None or (
                not merge_commit_oid.startswith(citation.cited_sha[:7])
                and not citation.cited_sha.startswith(merge_commit_oid[:7])
            ):
                check2_failure = (
                    f"{citation.pr_url} mergeCommit.oid="
                    f"{merge_commit_oid!r} does not match cited "
                    f"commit_sha={citation.cited_sha!r}. The contract is citing "
                    "a superseded or wrong PR — update dod_evidence to the actual "
                    "merge commit before re-running the gate."
                )
                break
        if check2_failure is None:
            cite_msg = (
                f"All {len(citations)} cited PR(s) are MERGED with matching SHAs."
                if citations
                else "Contract has no PR/commit citations to verify."
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

        # Check 3: OCC main contains a contract version with the cited merge commits
        # This catches the OMN-9855 case where main still has the stale contract
        # (citing #926) while the local contract has been updated to cite #949.
        main_contract = self._load_contract_on_ref(
            self._occ_repo_path, self._occ_main_ref, contract_rel_path
        )
        if main_contract is None:
            checks.append(
                ModelDurableEvidenceCheckResult(
                    check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                    passed=False,
                    message=(
                        f"Contract {contract_rel_path} is not present on "
                        f"{self._occ_main_ref}. Open an OCC PR with the contract "
                        "and merge it before transitioning Linear to Done."
                    ),
                )
            )
        else:
            main_citations = extract_cited_merge_commits(main_contract)
            local_keys = {(c.pr_url, c.cited_sha) for c in citations}
            main_keys = {(c.pr_url, c.cited_sha) for c in main_citations}
            if citations and not local_keys.issubset(main_keys):
                missing = local_keys - main_keys
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                        passed=False,
                        message=(
                            f"Contract on {self._occ_main_ref} is stale — "
                            f"missing citation(s) {sorted(missing)}. Open an OCC "
                            "PR to update the contract and merge it before "
                            "transitioning Linear to Done."
                        ),
                    )
                )
            else:
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                        passed=True,
                        message=(
                            f"Contract on {self._occ_main_ref} contains the "
                            "expected merge-commit citations."
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
        receipt_rel_path: str,
        contract_rel_path: str,
    ) -> ModelDurableEvidenceGateResult:
        """Run :meth:`evaluate` and raise on failure.

        On failure raises :class:`DurableEvidenceGateError` carrying the
        structured result. On success returns the result.
        """
        result = self.evaluate(
            ticket_id=ticket_id,
            contract=contract,
            receipt_rel_path=receipt_rel_path,
            contract_rel_path=contract_rel_path,
        )
        if result.status != EnumDurableEvidenceStatus.PASS:
            raise DurableEvidenceGateError(result)
        return result


__all__: list[str] = [
    "ContractOnRefLoader",
    "DurableEvidenceGate",
    "DurableEvidenceGateError",
    "GhPrViewProbe",
    "GitTrackedProbe",
    "extract_cited_merge_commits",
    "parse_pr_url",
]
