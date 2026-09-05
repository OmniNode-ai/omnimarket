# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17943 — retroactive diff-derived behavior-proof backfill for OCC contracts.

Why this file exists
--------------------
``handler_evidence_autoclose_sweep.py`` short-circuits on
``if behavior_proving_count <= 0`` BEFORE every other conjunct, and returns an
unconditional gap. That count is a property of the CONTRACT, not of the work:
a contract that declares no behavior-class ``dod_evidence`` item is pinned at
zero forever, no matter how well the merged code is tested.

The behavior-proof item ``dod-occ-diff-derived-behavior-proof`` (OMN-16434) was
introduced on 2026-08-28 and only 297 of 8,615 OCC contracts declare it.
Measured 2026-09-05 across the two beta sprint projects: of 238 open tickets,
125 carry OCC receipts and only 81 carry a behavior-proof receipt. The
remaining **44 carry receipts and zero behavior proof** — contracts that
PREDATE the minter. They were never judged and refused; they were born before
the judge existed, and nothing in the pipeline goes back for them. OMN-16859
owns the forward half (the producer's ``check_type``); this is the retroactive
half.

What this is NOT
----------------
It is not a new rule and not a new evidence class. Every derivation is
imported from the forward producer ``occ_evidence_stamp`` —
:func:`derive_behavior_test_paths`, :func:`behavior_proof_check_value`,
:func:`behavior_proof_cwd`, :func:`render_behavior_proof_dod_evidence_item` —
so a backfilled item and a forward-minted item cannot say different things
about the same PR. If the producer's rendering changes, this follows it or the
tests fail.

The refusals are the load-bearing half
--------------------------------------
A backfill that mints generously is a machine for manufacturing evidence. Four
refusals bound it, and each one exists because minting anyway would be worse
than the gap:

``REFUSED_NO_BEHAVIOUR_IN_DIFF``
    The merged PR's diff carries no pytest COLLECTION TARGET. The one thing on
    a product PR that is behavior proof by construction is the test the PR
    itself adds or changes; a file under ``tests/`` that pytest will not
    collect (``conftest.py``, a fixture) would mint a command that collects
    nothing and passes vacuously — exactly the class of check OMN-16434 exists
    to remove. Nothing is written.

``REFUSED_LEGACY_WHOLE_FILE_BINDING``
    A receipt already on this contract carries ``contract_sha256`` (whole-file)
    and no ``contract_entry_sha256``.
    ``check_receipt_hardening._contract_hash_violation`` validates the entry
    hash when present and falls back to ``sha256(contract file)`` when it is
    not — so appending ANY item to that contract turns a valid merged receipt
    into a "contract mutated after this receipt was produced" violation.
    Trading one gap for broken evidence is a net loss.

``REFUSED_PR_NOT_MERGED``
    No merge means no authoritative diff and no CI conclusion to read back.

``REFUSED_CONTRACT_SHAPE`` / ``REFUSED_NO_PRODUCT_PR``
    The contract has no ``dod_evidence`` block, or names no product PR. Fail
    closed rather than guess where an item belongs in a governance artifact.

Status is derived, never assumed
--------------------------------
``PASS`` requires a live readback saying the product PR is MERGED and its merge
commit's checks concluded successfully — which is precisely what
``contract_compliance_check._CHECK_RUNNERS['test_passes']`` asserts. Every
other case is ``PENDING``, which is non-PASS and leaves the ticket ineligible.
So this can widen the corpus of DECLARED bars without ever widening the corpus
of PASSED ones.

The minted receipt deliberately carries ``contract_entry_sha256`` and NO
whole-file ``contract_sha256``: minting one would seed the next generation of
``REFUSED_LEGACY_WHOLE_FILE_BINDING`` above.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# The yamlfmt-stable receipt serializer (OMN-17794). Imported, never copied:
# the two rewrites it defends against — go-yaml's line marker eating a newline
# out of captured stdout, and a lost keep indicator deleting trailing blank
# lines — change a receipt's VALUE, and a second implementation of that logic
# would drift out of agreement with the OCC formatter one repo over.
#
# It is a sibling SCRIPT, resolved at runtime by the sys.path insert above.
# The repo's mypy scope is `src/omnimarket` (ci.yml line 597 and the
# pre-commit mypy hook), so scripts/ci is not on a path mypy follows by
# default. Type-check this file with the directory on MYPYPATH:
#     MYPYPATH=scripts/ci uv run mypy scripts/ci/occ_behavior_proof_backfill.py --strict
# which passes clean. Deliberately NOT a `type: ignore`: nothing about this
# module's own types needs suppressing, and an ignore here would also hide a
# real signature change in the renderer.
from occ_receipt_runner import render_receipt_yaml  # noqa: E402
from omnibase_core.validation.validator_receipt_gate import (  # noqa: E402
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (  # noqa: E402
    BEHAVIOR_PROOF_EVIDENCE_ID,
    behavior_proof_check_value,
    derive_behavior_test_paths,
    render_behavior_proof_dod_evidence_item,
)

__all__ = [
    "BackfillOutcome",
    "EnumBackfillDecision",
    "ProductPrFacts",
    "ProductPrRef",
    "append_dod_evidence_item",
    "build_backfill_receipt",
    "decide",
    "derive_receipt_status",
    "discover_candidate_tickets",
    "legacy_whole_file_receipts",
    "main",
    "product_pr_from_contract",
    "repo_from_check_value",
    "run",
]

RECEIPT_SCHEMA_VERSION = "1.0.0"

# `runner` and `verifier` MUST differ: ModelDodReceipt's Centralized Transition
# Policy silently downgrades a PASS to ADVISORY when they match, and ADVISORY is
# non-PASS — a self-attested receipt would leave the gap open while looking
# like it had been closed. Neither name is on
# `check_receipt_hardening.DENYLISTED_VERIFIERS` (checked against OCC dev).
RUNNER = "omnimarket-ci occ-behavior-proof-backfill"
VERIFIER = "github-actions merged-pr diff derivation"

# `dod-<owner>-<repo>-pr-<n>` is the id the autobind gives the binding item, so
# the contract already names its own product PR. Reading it from there avoids a
# second source of truth (the alternative — grepping OCC commit subjects for
# `evidence(OMN-XXXX)` — is an index of claims, not of content). The `-ci`
# sibling names the SAME PR and is skipped so it cannot produce a competing
# answer.
#
# The id supplies the PR NUMBER only. It cannot supply the repo: both halves of
# `OmniNode-ai/omnibase_infra` contain hyphens, so `dod-OmniNode-ai-omnibase_infra-pr-3014`
# has no unambiguous split — the first draft of this module read that owner as
# `OmniNode` and the repo as `ai-omnibase_infra`, caught by
# `test_the_product_pr_is_read_out_of_the_contract_not_guessed`. The repo is
# therefore taken from the item's own `check_value`, where it appears as a
# `--repo <owner>/<name>` argument and is unambiguous by construction.
_PRODUCT_PR_ID_RE = re.compile(r"^dod-.+-pr-(?P<pr>\d+)$")

# Two check_value shapes carry the repo, and BOTH are common in the live corpus.
# MEASURED on OCC dev over the 3,880 candidate contracts (have receipts, no
# behavior-proof receipt): 537 name it as `--repo <owner>/<name>`, and a further
# 277 name it ONLY inside a `gh api repos/<owner>/<name>/contents/...` path.
# Accepting just the first form refused those 277 as REFUSED_NO_PRODUCT_PR — a
# fail-closed refusal rather than a wrong mint, but 34% of the resolvable corpus
# left unreachable for a parsing reason rather than an evidentiary one. Found by
# the first live CI run of the scheduled workflow, which reported 17 of 25
# discovered candidates as REFUSED_NO_PRODUCT_PR; two of them were then read by
# hand and only one was genuinely PR-less.
#
# `--repo` is tried FIRST and wins: on a contract carrying both, it is the
# explicit statement of which repo the PR lives in, whereas an api path may
# reference some other repo's contents.
_REPO_ARG_RE = re.compile(r"--repo\s+(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)")
_REPO_API_PATH_RE = re.compile(r"\brepos/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/")


def repo_from_check_value(check_value: str) -> str | None:
    """The product repo a check names, from either shape, `--repo` first."""
    explicit = _REPO_ARG_RE.search(check_value)
    if explicit is not None:
        return explicit.group("repo")
    api_path = _REPO_API_PATH_RE.search(check_value)
    if api_path is not None:
        return api_path.group("repo")
    return None


# Conclusions that do not mean "this merge was proven red". `skipped` and
# `neutral` are how a conditional job reports that it had nothing to do.
_NON_FAILING_CHECK_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

_GH_TIMEOUT_SECONDS = 120


class EnumBackfillDecision(StrEnum):
    """What the backfill decided for one ticket, and why."""

    MINT = "MINT"
    REFUSED_ALREADY_DECLARED = "REFUSED_ALREADY_DECLARED"
    REFUSED_CONTRACT_SHAPE = "REFUSED_CONTRACT_SHAPE"
    REFUSED_NO_PRODUCT_PR = "REFUSED_NO_PRODUCT_PR"
    REFUSED_PR_NOT_MERGED = "REFUSED_PR_NOT_MERGED"
    REFUSED_NO_BEHAVIOUR_IN_DIFF = "REFUSED_NO_BEHAVIOUR_IN_DIFF"
    REFUSED_LEGACY_WHOLE_FILE_BINDING = "REFUSED_LEGACY_WHOLE_FILE_BINDING"


@dataclass(frozen=True)
class ProductPrRef:
    """The product PR a contract names."""

    repo: str
    pr_number: int


@dataclass(frozen=True)
class ProductPrFacts:
    """Everything read back live about one product PR.

    Separated from the resolution so every decision in this module is a pure
    function of observed facts, testable without a network.
    """

    repo: str
    pr_number: int
    state: str
    merge_commit_sha: str
    head_sha: str
    head_ref: str
    changed_files: tuple[str, ...]
    checks_conclusion: str
    checks_probe_stdout: str


@dataclass(frozen=True)
class BackfillOutcome:
    """The plan for one ticket. The writer consumes this and nothing else."""

    ticket_id: str
    decision: EnumBackfillDecision
    reason: str
    repo: str = ""
    pr_number: int = 0
    test_paths: tuple[str, ...] = ()
    receipt_status: str = ""
    contract_item_text: str | None = None
    receipt_body: dict[str, Any] | None = None

    def as_report_row(self) -> dict[str, Any]:
        """The JSON row a run emits — every field a reader needs to check it."""
        return {
            "ticket_id": self.ticket_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "test_paths": list(self.test_paths),
            "receipt_status": self.receipt_status,
        }


# ---------------------------------------------------------------------------
# Pure predicates
# ---------------------------------------------------------------------------


def product_pr_from_contract(contract_data: Any) -> ProductPrRef | None:
    """The product PR this contract binds, read out of its own dod_evidence ids.

    Returns ``None`` rather than guessing when no item matches. A contract
    carrying only ``occ-self-bind-pr-<n>`` names the OCC companion, not a
    product PR, and must not be mistaken for one — the id prefix `dod-` is what
    separates the two.

    Both halves must resolve. An id that looks right but whose check names no
    ``--repo`` argument yields ``None``, which routes to
    ``REFUSED_NO_PRODUCT_PR`` rather than to a half-known reference.
    """
    if not isinstance(contract_data, dict):
        return None
    items = contract_data.get("dod_evidence")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or raw_id.endswith("-ci"):
            continue
        match = _PRODUCT_PR_ID_RE.match(raw_id)
        if match is None:
            continue
        checks = item.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            value = check.get("check_value")
            if not isinstance(value, str):
                continue
            repo = repo_from_check_value(value)
            if repo is None:
                continue
            return ProductPrRef(repo=repo, pr_number=int(match.group("pr")))
    return None


def contract_declares_behavior_proof(contract_data: Any) -> bool:
    """True when the behavior-proof item is already on the contract."""
    if not isinstance(contract_data, dict):
        return False
    items = contract_data.get("dod_evidence")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict) and item.get("id") == BEHAVIOR_PROOF_EVIDENCE_ID
        for item in items
    )


def legacy_whole_file_receipts(
    receipts: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Paths of receipts whose contract binding is the whole-file hash.

    The predicate is the ABSENCE of ``contract_entry_sha256``, because that is
    exactly what ``_contract_hash_violation`` branches on: entry hash first,
    whole-file only as the fallback. A receipt carrying both is safe — the
    validator never reaches the fallback for it.
    """
    stale: list[str] = []
    for path, body in sorted(receipts.items()):
        if not isinstance(body, Mapping):
            continue
        if body.get("contract_entry_sha256") is None and body.get("contract_sha256"):
            stale.append(path)
    return tuple(stale)


def derive_receipt_status(pr_facts: ProductPrFacts) -> str:
    """``PASS`` only for a merged PR whose merge checks concluded successfully.

    This is not a softening of ``test_passes`` — it is its declared meaning.
    ``contract_compliance_check._CHECK_RUNNERS['test_passes']`` ignores
    ``check_value`` and asserts THE PR'S OWN CI is green; for a MERGED PR that
    fact is settled and readable. Anything short of it is ``PENDING``, which is
    non-PASS, so a ticket whose CI was not green gains a declared bar and no
    passing proof.

    "The PR's own CI" is the check-runs on its HEAD sha, not on the squash
    commit. MEASURED, not assumed: on ``omninode_infra#1041`` the head sha
    carries 30 success and nothing else, while the merge commit on ``dev``
    carries 1 failure among 23 — a post-merge deploy workflow that ran after
    the code landed and says nothing about the tests. Reading the merge commit
    marked three of four live candidates PENDING for reasons unrelated to their
    tests, which is how this was caught.
    """
    if pr_facts.state != "MERGED":
        return "PENDING"
    if pr_facts.checks_conclusion in _NON_FAILING_CHECK_CONCLUSIONS:
        return "PASS"
    return "PENDING"


def append_dod_evidence_item(contract_text: str, item_text: str) -> str:
    """Insert ``item_text`` at the end of the contract's ``dod_evidence`` block.

    Textual, not a YAML round-trip, and deliberately so: re-dumping a contract
    reflows every other entry, which restales every ``contract_entry_sha256``
    on the ticket — the same class of damage
    :data:`EnumBackfillDecision.REFUSED_LEGACY_WHOLE_FILE_BINDING` exists to
    prevent, inflicted by the repair instead of by the gap.

    The block END is found by scanning for the next line at column zero, so a
    ``dod_evidence`` block in the middle of a hand-authored contract is handled
    correctly. Appending at end-of-file would corrupt those.
    """
    lines = contract_text.splitlines(keepends=True)
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("dod_evidence:"):
            start = index
            break
    if start is None:
        raise ValueError(
            "contract declares no top-level `dod_evidence:` block; refusing to "
            "guess where a governance item belongs."
        )

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = index
            break
    # Trailing blank lines inside the block belong after the new item, not
    # before it.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1

    body = item_text if item_text.endswith("\n") else item_text + "\n"
    return "".join(lines[:end]) + body + "".join(lines[end:])


def build_backfill_receipt(
    *,
    ticket_id: str,
    pr_facts: ProductPrFacts,
    test_paths: Sequence[str],
    contract_entry_sha256: str,
    status: str,
    run_url: str,
    run_timestamp: datetime | None = None,
) -> dict[str, Any]:
    """The receipt body for one backfilled behavior-proof item.

    ``check_value`` is the declared bar, produced by the forward producer's own
    :func:`behavior_proof_check_value`. ``probe_command`` is the readback that
    justified :func:`derive_receipt_status` — a different statement from the
    bar, and named as such in ``actual_output`` so nobody reads this receipt as
    a claim that the pytest run was executed here. It was not; it was executed
    by the product PR's own CI before it merged, and this receipt records the
    observation of that fact.

    No ``contract_sha256``. See the module docstring.
    """
    stamped = run_timestamp or datetime.now(tz=UTC)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ticket_id": ticket_id,
        "evidence_item_id": BEHAVIOR_PROOF_EVIDENCE_ID,
        "check_type": "test_passes",
        "check_value": behavior_proof_check_value(test_paths),
        "contract_entry_sha256": contract_entry_sha256,
        "status": status,
        "run_timestamp": stamped.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit_sha": pr_facts.merge_commit_sha,
        "runner": RUNNER,
        "verifier": VERIFIER,
        "probe_command": (
            f"gh api repos/{pr_facts.repo}/commits/{pr_facts.head_sha}"
            "/check-runs --paginate --jq "
            '\'[.check_runs[]|select(.conclusion|IN("success","skipped",'
            '"neutral")|not)]|length\''
        ),
        "probe_stdout": pr_facts.checks_probe_stdout,
        "actual_output": (
            f"{status}: the declared targets are derived from the merged diff of "
            f"{pr_facts.repo}#{pr_facts.pr_number}; the PR's own CI at head "
            f"{pr_facts.head_sha} concluded "
            f"{pr_facts.checks_conclusion or 'UNREADABLE'}; it merged as "
            f"{pr_facts.merge_commit_sha}. This receipt records that observation "
            "— it does not claim the pytest run was executed by the backfill. "
            f"Run: {run_url}"
        ),
        "exit_code": 0,
        "pr_number": pr_facts.pr_number,
        "branch": pr_facts.head_ref,
        # The CI workspace path is machine-specific and unreproducible, and
        # OCC's Receipt Honesty Gate rejects one.
        "working_dir": None,
    }


def decide(
    *,
    ticket_id: str,
    contract_text: str,
    existing_receipts: Mapping[str, Mapping[str, Any]],
    behavior_receipt_exists: bool,
    pr_facts: ProductPrFacts | None,
) -> BackfillOutcome:
    """The whole judgement for one ticket, as a pure function of observed facts.

    Order matters and is chosen so the cheapest refusal that is also the most
    informative comes first: an already-declared contract is a no-op (the
    backfill is idempotent and safe to re-run), and only then do the refusals
    that describe a real gap get evaluated.
    """
    try:
        contract_data = yaml.safe_load(contract_text)
    except yaml.YAMLError as exc:
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_CONTRACT_SHAPE,
            reason=f"contract YAML does not parse: {exc}",
        )

    if contract_declares_behavior_proof(contract_data) or behavior_receipt_exists:
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_ALREADY_DECLARED,
            reason=(
                f"{BEHAVIOR_PROOF_EVIDENCE_ID} is already declared or already has "
                "a receipt; the backfill is idempotent and writes nothing."
            ),
        )

    if not isinstance(contract_data, dict) or not isinstance(
        contract_data.get("dod_evidence"), list
    ):
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_CONTRACT_SHAPE,
            reason=(
                "contract declares no top-level `dod_evidence` list; refusing to "
                "guess where a governance item belongs."
            ),
        )

    ref = product_pr_from_contract(contract_data)
    if ref is None or pr_facts is None:
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_NO_PRODUCT_PR,
            reason=(
                "no `dod-<owner>-<repo>-pr-<n>` item names a product PR, so there "
                "is no diff to derive a behavior proof from."
            ),
        )

    stale = legacy_whole_file_receipts(existing_receipts)
    if stale:
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_LEGACY_WHOLE_FILE_BINDING,
            reason=(
                f"{len(stale)} existing receipt(s) bind this contract by whole-file "
                "contract_sha256 with no contract_entry_sha256 "
                f"({stale[0]}); appending an item would restale that binding and "
                "turn valid merged evidence into a hash mismatch. Refusing to "
                "trade a gap for broken evidence."
            ),
            repo=ref.repo,
            pr_number=ref.pr_number,
        )

    if pr_facts.state != "MERGED":
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_PR_NOT_MERGED,
            reason=(
                f"{ref.repo}#{ref.pr_number} is {pr_facts.state or 'UNKNOWN'}, not "
                "MERGED; there is no authoritative diff and no settled CI "
                "conclusion to read back."
            ),
            repo=ref.repo,
            pr_number=ref.pr_number,
        )

    test_paths = derive_behavior_test_paths(pr_facts.changed_files)
    if not test_paths:
        return BackfillOutcome(
            ticket_id=ticket_id,
            decision=EnumBackfillDecision.REFUSED_NO_BEHAVIOUR_IN_DIFF,
            reason=(
                f"the merged diff of {ref.repo}#{ref.pr_number} carries no pytest "
                f"collection target across {len(pr_facts.changed_files)} changed "
                "file(s). Naming any other test would assert behavior this PR "
                "never demonstrated; nothing is minted."
            ),
            repo=ref.repo,
            pr_number=ref.pr_number,
        )

    item_text = render_behavior_proof_dod_evidence_item(
        repo=ref.repo, pr_number=ref.pr_number, test_paths=test_paths
    )
    return BackfillOutcome(
        ticket_id=ticket_id,
        decision=EnumBackfillDecision.MINT,
        reason=(
            f"derived {len(test_paths)} pytest target(s) from the merged diff of "
            f"{ref.repo}#{ref.pr_number}: {', '.join(test_paths)}."
        ),
        repo=ref.repo,
        pr_number=ref.pr_number,
        test_paths=test_paths,
        receipt_status=derive_receipt_status(pr_facts),
        contract_item_text=item_text,
    )


# ---------------------------------------------------------------------------
# I/O — the only impure surface
# ---------------------------------------------------------------------------


def _gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` and parse JSON, returning ``None`` on any failure.

    stderr is NOT suppressed: a sweep whose errors are discarded returns zero
    rows and reads exactly like a clean result. A failure here degrades to a
    refusal (fewer facts means a refusal branch), never to a fabricated mint.
    """
    try:
        completed = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"::warning::gh {' '.join(args)} failed: {exc}", file=sys.stderr)
        return None
    if completed.returncode != 0:
        print(
            f"::warning::gh {' '.join(args)} exited {completed.returncode}: "
            f"{completed.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError):
        return None


def resolve_pr_facts(ref: ProductPrRef) -> ProductPrFacts | None:
    """Read back everything the decision needs about one product PR."""
    view = _gh_json(
        [
            "pr",
            "view",
            str(ref.pr_number),
            "--repo",
            ref.repo,
            "--json",
            "state,mergeCommit,headRefOid,headRefName,files",
        ]
    )
    if not isinstance(view, dict):
        return None

    merge_commit = view.get("mergeCommit")
    merge_sha = ""
    if isinstance(merge_commit, dict):
        merge_sha = str(merge_commit.get("oid") or "")

    files: list[str] = []
    raw_files = view.get("files")
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("path") or entry.get("filename")
            if isinstance(raw, str) and raw:
                files.append(raw)

    head_sha = str(view.get("headRefOid") or "")

    # The PR's OWN CI lives on its head sha. The squash commit on `dev` carries
    # whatever ran AFTER the merge — deploys, nightlies, release syncs — whose
    # failures say nothing about the tests this receipt is about.
    conclusion = ""
    probe_stdout = ""
    if head_sha:
        runs = _gh_json(
            [
                "api",
                f"repos/{ref.repo}/commits/{head_sha}/check-runs",
                "--paginate",
                "--jq",
                ".check_runs",
            ]
        )
        if isinstance(runs, list):
            failing = [
                str(entry.get("name"))
                for entry in runs
                if isinstance(entry, dict)
                and str(entry.get("conclusion") or "")
                not in _NON_FAILING_CHECK_CONCLUSIONS
            ]
            conclusion = "success" if not failing else "failure"
            probe_stdout = json.dumps(
                {"total": len(runs), "failing": len(failing), "names": failing[:8]},
                sort_keys=True,
            )

    return ProductPrFacts(
        repo=ref.repo,
        pr_number=ref.pr_number,
        state=str(view.get("state") or ""),
        merge_commit_sha=merge_sha,
        head_sha=head_sha,
        head_ref=str(view.get("headRefName") or ""),
        changed_files=tuple(files),
        checks_conclusion=conclusion,
        checks_probe_stdout=probe_stdout,
    )


def discover_candidate_tickets(occ_root: Path, *, limit: int) -> tuple[str, ...]:
    """Tickets that HAVE OCC receipts and no behavior-proof receipt.

    "Has receipts" is the filter that separates a contract the pipeline already
    touched from one that was never bound at all; the latter is a different
    problem (no companion) and not this mechanism's business.

    Ordered by ticket number DESCENDING — newest first. That is deliberate and
    is not a proxy for importance: the newest contracts are the ones closest to
    the minter's introduction, so they are the most likely to be a pure
    predates-the-minter gap rather than a contract with some older structural
    problem. It is also stable across runs, so a bounded run is resumable
    instead of re-deciding the same head of the list forever — each applied
    batch removes its own tickets from the next run's candidates.
    """
    base = occ_root / "drift" / "dod_receipts"
    if not base.is_dir():
        return ()
    candidates: list[tuple[int, str]] = []
    for ticket_dir in base.iterdir():
        if not ticket_dir.is_dir():
            continue
        name = ticket_dir.name
        if not name.startswith("OMN-") or not name[4:].isdigit():
            continue
        if (ticket_dir / BEHAVIOR_PROOF_EVIDENCE_ID).is_dir():
            continue
        if not any(ticket_dir.rglob("*.yaml")):
            continue
        if not (occ_root / "contracts" / f"{name}.yaml").is_file():
            continue
        candidates.append((int(name[4:]), name))
    candidates.sort(reverse=True)
    return tuple(name for _, name in candidates[:limit])


def _load_receipts(occ_root: Path, ticket_id: str) -> dict[str, dict[str, Any]]:
    """Every receipt currently filed under this ticket, keyed by repo-rel path."""
    base = occ_root / "drift" / "dod_receipts" / ticket_id
    receipts: dict[str, dict[str, Any]] = {}
    if not base.is_dir():
        return receipts
    for path in sorted(base.rglob("*.yaml")):
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(body, dict):
            receipts[str(path.relative_to(occ_root))] = body
    return receipts


def run(
    *,
    occ_root: Path,
    tickets: Sequence[str],
    apply: bool,
    run_url: str,
    limit: int,
) -> dict[str, Any]:
    """Plan (and optionally write) the backfill for each ticket, in order."""
    outcomes: list[BackfillOutcome] = []
    written = 0

    for ticket_id in tickets:
        contract_path = occ_root / "contracts" / f"{ticket_id}.yaml"
        if not contract_path.is_file():
            outcomes.append(
                BackfillOutcome(
                    ticket_id=ticket_id,
                    decision=EnumBackfillDecision.REFUSED_CONTRACT_SHAPE,
                    reason=f"no contract at contracts/{ticket_id}.yaml",
                )
            )
            continue

        contract_text = contract_path.read_text(encoding="utf-8")
        receipts = _load_receipts(occ_root, ticket_id)
        behavior_dir = (
            occ_root / "drift" / "dod_receipts" / ticket_id / BEHAVIOR_PROOF_EVIDENCE_ID
        )

        try:
            contract_data = yaml.safe_load(contract_text)
        except yaml.YAMLError:
            contract_data = None
        ref = product_pr_from_contract(contract_data)
        pr_facts = resolve_pr_facts(ref) if ref is not None else None

        outcome = decide(
            ticket_id=ticket_id,
            contract_text=contract_text,
            existing_receipts=receipts,
            behavior_receipt_exists=behavior_dir.is_dir(),
            pr_facts=pr_facts,
        )
        outcomes.append(outcome)

        if outcome.decision is not EnumBackfillDecision.MINT:
            continue
        if written >= limit:
            continue
        if not apply:
            written += 1
            continue

        assert outcome.contract_item_text is not None
        assert pr_facts is not None
        new_text = append_dod_evidence_item(contract_text, outcome.contract_item_text)
        new_data = yaml.safe_load(new_text)
        entry_sha = compute_contract_entry_sha256(new_data, BEHAVIOR_PROOF_EVIDENCE_ID)
        receipt = build_backfill_receipt(
            ticket_id=ticket_id,
            pr_facts=pr_facts,
            test_paths=outcome.test_paths,
            contract_entry_sha256=entry_sha,
            status=outcome.receipt_status,
            run_url=run_url,
        )
        # Render the receipt BEFORE touching the contract: a body that cannot
        # be represented faithfully must leave the tree untouched, not a
        # mutated contract with no receipt behind it.
        receipt_text = render_receipt_yaml(receipt)
        contract_path.write_text(new_text, encoding="utf-8")
        behavior_dir.mkdir(parents=True, exist_ok=True)
        (behavior_dir / "test_passes.yaml").write_text(receipt_text, encoding="utf-8")
        written += 1

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.decision.value] = counts.get(outcome.decision.value, 0) + 1

    return {
        "dry_run": not apply,
        "tickets_scanned": len(tickets),
        "written": written if apply else 0,
        "would_write": written if not apply else 0,
        "limit": limit,
        "counts": counts,
        "outcomes": [outcome.as_report_row() for outcome in outcomes],
    }


def _tickets_from(text: str) -> tuple[str, ...]:
    """Every ``OMN-<n>`` in ``text``, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for match in re.findall(r"OMN-[0-9]+", text or ""):
        seen.setdefault(match, None)
    return tuple(seen)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the diff-derived behavior-proof dod_evidence item onto OCC "
            "contracts that predate the minter (OMN-17943)."
        )
    )
    parser.add_argument("--occ-root", required=True, type=Path)
    parser.add_argument(
        "--tickets",
        default="",
        help="Whitespace- or comma-separated OMN ids to consider.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Take the candidate list from the OCC tree instead of --tickets: "
            "tickets that have receipts and no behavior-proof receipt."
        ),
    )
    parser.add_argument(
        "--discover-limit",
        type=int,
        default=40,
        help="How many discovered candidates to consider (not to mint).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the mints. Omitted, the run reports what it would do.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help=(
            "Maximum mints per run. Bounded on purpose: a governance PR nobody "
            "can read is a governance PR nobody checks."
        ),
    )
    parser.add_argument("--run-url", default="")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.discover:
        tickets = discover_candidate_tickets(
            args.occ_root, limit=int(args.discover_limit)
        )
    else:
        tickets = _tickets_from(args.tickets)
    if not tickets:
        print("::notice::No candidate tickets; nothing to do.")
        return 0

    report = run(
        occ_root=args.occ_root,
        tickets=tickets,
        apply=bool(args.apply),
        run_url=str(args.run_url),
        limit=int(args.limit),
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
