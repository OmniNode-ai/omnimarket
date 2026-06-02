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
2. The contract's ``dod_evidence`` cites a ``pr_url`` whose state is not
   ``MERGED`` or whose ``mergeCommit.oid`` does not match the cited SHA.
3. The contract version on the OCC governance ref does not yet contain the
   real merge commit citation (i.e. the ref still has the stale contract).

The gate is pure logic plus pluggable Protocol probes (git receipt-tracked,
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
    seen: set[tuple[str, int, str]] = set()
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
            # Skip malformed SHAs (the docstring contracts that malformed
            # citations are skipped, not raised). Without this guard a sha
            # like "abc" reaches ModelCitedMergeCommit and raises
            # ValidationError on min_length=7.
            if not _SHA_RE.match(sha):
                continue
            parsed = parse_pr_url(pr_url)
            if parsed is None:
                continue
            repo, num = parsed
            # Dedupe on the normalized identity (repo, pr_number, sha) — NOT
            # the raw pr_url. parse_pr_url() accepts multiple URL shapes for
            # the same PR (e.g. ``/pull/123`` vs ``/pull/123/files``); keying
            # on raw pr_url would treat them as distinct citations and
            # double-call gh_pr_view AND falsely hard-fail
            # CONTRACT_ON_OCC_MAIN when local and OCC main spell the same
            # PR differently.
            key = (repo, num, sha)
            if key in seen:
                continue
            seen.add(key)
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
        occ_repo_path: str,
        occ_governance_ref: str = DEFAULT_OCC_GOVERNANCE_REF,
    ) -> None:
        self._is_receipt_tracked = is_receipt_tracked
        self._gh_pr_view = gh_pr_view
        self._load_contract_on_ref = load_contract_on_ref
        self._occ_repo_path = occ_repo_path
        self._occ_governance_ref = occ_governance_ref

    def evaluate_default(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
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
        )

    def evaluate(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        receipt_dir: str,
        contract_rel_path: str,
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

        # Check 3: the OCC governance ref contains a contract version with the
        # cited merge commits. This catches the OMN-9855 case where the ref
        # still has the stale contract (citing #926) while the local contract
        # has been updated to cite #949.
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
            main_citations = extract_cited_merge_commits(main_contract)
            # Compare on normalized identity (repo, pr_number, sha) — NOT raw
            # pr_url — so different URL spellings for the same PR do not
            # produce a false stale-ref hard fail.
            local_keys = {(c.repo, c.pr_number, c.cited_sha) for c in citations}
            main_keys = {(c.repo, c.pr_number, c.cited_sha) for c in main_citations}
            if citations and not local_keys.issubset(main_keys):
                missing = local_keys - main_keys
                checks.append(
                    ModelDurableEvidenceCheckResult(
                        check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                        passed=False,
                        message=(
                            f"Contract on {self._occ_governance_ref} is stale — "
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
                            f"Contract on {self._occ_governance_ref} contains "
                            "the expected merge-commit citations."
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
        )
        if result.status != EnumDurableEvidenceStatus.PASS:
            raise DurableEvidenceGateError(result)
        return result

    def enforce_default(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
    ) -> ModelDurableEvidenceGateResult:
        """Run :meth:`evaluate_default` and raise on failure.

        This is the DEFAULT hard-fail invocation: it resolves the canonical
        platform paths internally. On failure raises
        :class:`DurableEvidenceGateError` carrying the structured result. On
        success returns the result.
        """
        result = self.evaluate_default(ticket_id=ticket_id, contract=contract)
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
    "default_contract_path",
    "default_receipt_dir",
    "extract_cited_merge_commits",
    "parse_pr_url",
]
