# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""DurableEvidenceGate — pre-Linear-Done verification of the durable evidence trail.

The OMN-9855 incident (2026-04-30) closed a Linear ticket as Done on evidence
that existed only locally, so Linear was green while the durable record did not
back it. The gate refuses the Done transition unless the durable record is the
ticket's product-repository contract and the check run on the merged head.

Product repository only (OMN-20074)
-----------------------------------
A ticket is decided on the contract its merged PRs carry in their own
repository, ``contracts/<TICKET>.yaml``, read the way the omniclaude Done gate
reads it: at each merged PR's merge commit; of several merged PRs in one
repository that carry it, the newest merged one decides; the verdict is the
``repo-evidence / dod-verify`` check run on that PR's head, whose contract must
equal the merged one; and every labelled criterion of the ticket must be bound.
A ticket no merged PR governs this way is refused with the reason the read gave.
No other repository, clone or ref is read in its place. See
:func:`evaluate_repo_evidence`.

The gate is pure logic plus pluggable Protocol probes (the released-is-Done
probes and the two product-repository readers). Tests inject deterministic
stubs; production wiring is :class:`HandlerDurableEvidenceGateEffect`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from html import escape
from typing import Protocol

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDefectLabel,
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    EnumRepoEvidenceOutcome,
    ModelCitedMergeCommit,
    ModelDurableEvidenceCheckResult,
    ModelDurableEvidenceGateResult,
    ModelEngagedRepoContract,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
    ModelRepoEvidenceVerdict,
    ModelTicketMergedPr,
)
from omnimarket.nodes.node_dod_verify.services.ac_binding_retirements import (
    resolve_retirements,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import criteria_revision
from omnimarket.nodes.node_dod_verify.services.released_evidence import (
    EnumReleasedOutcome,
    IndexReleaseFilesProbe,
    ModelReleasedCitationInput,
    ReleaseTagsContainingProbe,
    evaluate_released,
    is_publishing_repo,
)
from omnimarket.occ_contract_pin import acceptance_criteria_items, canonical_ac_label

_PR_URL_RE = re.compile(
    "^"
    + re.escape(service_endpoint_url("github.web_url"))
    + r"/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>\d+)"
)

# Git short SHA is 7 hex chars; full is 40. Anything outside [7,40] hex chars
# is treated as a malformed citation and skipped (see CR thread on PR #467).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# OMN-13888: the unforgeable ``.supersede.<TOKEN>.yaml`` filename ordinal.
# Identical to the omnibase_core ``resolve_supersession`` authority key so both
# resolvers agree on which supersession record is "latest".
#
# OMN-19121: the token is everything between ``.supersede.`` and the extension,
# DOTS INCLUDED. The prior ``(\d+)`` excluded them, so an attempt-scoped record
# -- ``<check>.supersede.<pr>.<n>.yaml``, which a re-executed check has minted
# since OMN-19050 -- produced no ordinal at all. It then sorted BELOW the bare
# record it was filed to correct, and this gate read the superseded FAIL while
# the binding resolver read the correcting PASS. Measured on the live merged
# chain of OMN-18868 / omnimarket#2751, with no synthetic record: this gate
# selected ``test_passes.supersede.2751.yaml`` (FAIL at bab99887) where
# ``resolve_supersession`` selected ``test_passes.supersede.2751.0002.yaml``
# (PASS at b5c6f7a4). Because Check 2 keeps only ``status == "PASS"`` receipts,
# the key contributed nothing and the Done flip reported no PASS receipt
# binding the PR.
_SUPERSEDE_SEQ_RE = re.compile(r"\.supersede\.([^/]+)\.yaml$")


def _supersede_sequence(token: str | None) -> tuple[int, ...] | None:
    """Total order over a dotted-numeric supersede token, or None.

    ``"2751"`` to ``(2751,)`` and ``"2751.0002"`` to ``(2751, 2)``, so an
    attempt-scoped record sorts strictly after the bare-PR record it extends by
    plain tuple comparison. A single-component token keys to a 1-tuple and
    compares exactly as the pre-OMN-19121 ``int(token)`` did, so every chain
    that has only ever used one resolves unchanged.

    Mirrors ``omnibase_core.validation.validator_receipt_supersession``'s
    ``_sequence_key`` field for field. It is written out here rather than
    imported because this repository pins ``omnibase-core`` from the registry
    and the lock resolves ``0.47.20``, which does not carry that helper.
    Replacing this with the import once the lock moves is OMN-19121 AC3/AC4.
    """
    if token is None:
        return None
    parts = token.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


# Canonical contract path prefix relative to the OCC repo root. A product
# repository that owns its evidence uses the same layout (OMN-20071).
_CONTRACT_DIR_PREFIX = "contracts"

# The repository-owned verdict (OMN-20071): the check run the product
# repository's ``repo-evidence`` caller workflow reports on a PR head, from the
# workflow the PR cannot edit. Matches the omniclaude Done gate's constants.
REPO_EVIDENCE_CHECK_NAME = "repo-evidence / dod-verify"
REPO_EVIDENCE_APP_SLUG = "github-actions"


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


def _sha_prefix_match(a: str, b: str) -> bool:
    """Bidirectional git short-SHA (7-char) prefix match between two hex SHAs.

    A receipt may cite a 7-40 char SHA and either side may be the abbreviated
    form. Pure function — no I/O.
    """
    if not a or not b:
        return False
    return a.startswith(b[:7]) or b.startswith(a[:7])


class RepoContractReader(Protocol):
    """Probe: ``contracts/<ticket_id>.yaml`` of ``repo`` at commit ``ref``.

    Production wiring reads ``repos/<repo>/contents/contracts/<ticket>.yaml``
    at the fixed commit through the GitHub REST API. ``ABSENT`` is a definite
    "this commit carries no contract"; ``ERROR`` is unreadable and refuses.
    """

    def __call__(
        self, repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead: ...


class RepoEvidenceCheckRunsReader(Protocol):
    """Probe: the latest check runs on ``sha`` of ``repo``, or ``None``.

    Production wiring reads ``repos/<repo>/commits/<sha>/check-runs`` with
    ``filter=latest`` and the :data:`REPO_EVIDENCE_CHECK_NAME` name filter.
    ``None`` means unreadable and refuses.
    """

    def __call__(
        self, repo: str, sha: str
    ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None: ...


def _diagnostic_text(value: object) -> str:
    """Render external evidence as inert, single-line diagnostic text.

    Contract fields, probe errors and ticket text are untrusted. Escape markup
    and quotes for display, and render control characters (including terminal
    escapes and Unicode line/direction controls) visibly instead of executing
    them or letting them forge a second log line. Only presentation changes;
    probes and contract comparisons always use the original values.
    """
    return escape(
        "".join(
            char if char.isprintable() else ascii(char)[1:-1] for char in str(value)
        )
    )


def _repo_contract_or_refusal(
    read: ModelRepoContractRead, ticket_id: str, source: str
) -> dict[str, object] | ModelRepoEvidenceVerdict:
    """The contract mapping of a read, or the refusal it amounts to."""
    if read.status is not EnumRepoContractReadStatus.FOUND or read.contract is None:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.REFUSED,
            detail=(
                f"{_diagnostic_text(source)}: "
                f"{_diagnostic_text(default_contract_path(ticket_id))} is {read.status.value}"
                f" ({_diagnostic_text(read.error or 'contract absent')}); restore readable evidence."
            ),
        )
    named = read.contract.get("ticket_id")
    if named is not None and named != ticket_id:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.REFUSED,
            detail=(
                f"{_diagnostic_text(source)}: contract names ticket_id "
                f"{_diagnostic_text(repr(named))}, not {_diagnostic_text(ticket_id)}; "
                "correct the ticket binding."
            ),
        )
    return read.contract


def _contract_bindings(contract: dict[str, object]) -> dict[str, list[str]]:
    """``{criterion label: [item id, ...]}`` for every live ``binds_ac`` entry.

    A binding retired through ``ac_bindings`` (the node's own retirement
    resolution) does not count, so this reads the same bound set the node's
    unbound-criteria rule reads.
    """
    raw_items = contract.get("dod_evidence")
    items = (
        [item for item in raw_items if isinstance(item, dict)]
        if isinstance(raw_items, list)
        else []
    )
    retired = resolve_retirements(items).pairs
    bindings: dict[str, list[str]] = {}
    for item in items:
        item_id = item.get("id")
        raw_binds = item.get("binds_ac")
        if not isinstance(item_id, str) or not isinstance(raw_binds, list):
            continue
        for raw in raw_binds:
            label = canonical_ac_label(str(raw))
            if label and (item_id, label) not in retired:
                bindings.setdefault(label, []).append(item_id)
    return bindings


def resolve_repo_contracts(
    ticket_id: str,
    merged_prs: tuple[ModelTicketMergedPr, ...],
    *,
    read_repo_contract: RepoContractReader,
    read_repo_check_runs: RepoEvidenceCheckRunsReader,
) -> ModelRepoEvidenceVerdict | tuple[ModelEngagedRepoContract, ...]:
    """The product-repository contracts that govern a ticket, per repository.

    Steps 1 to 3 of :func:`evaluate_repo_evidence`, shared with
    ``node_dod_verify``'s contract lookup (OMN-20886) so the node and the gate
    resolve one ticket to the same contracts:

    1. Read ``contracts/<ticket>.yaml`` at every merged PR's merge commit. No PR
       carrying one is NOT_ENGAGED; an unreadable or malformed contract is
       REFUSED.
    2. Within one repository, only the newest merged contract-carrying PR
       decides. A PR without a merge time is never treated as superseded.
    3. A deciding PR with no ``repo-evidence / dod-verify`` run on its head has
       not adopted the repo path; when no deciding PR has one, NOT_ENGAGED.
       Unreadable check runs are REFUSED.

    Returns the engaged contracts, one per deciding PR, or the verdict that
    stops resolution there.
    """

    candidates = {(pr.repo, pr.pr_number): pr for pr in merged_prs}
    if not candidates:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.NOT_ENGAGED,
            detail="no merged product PR to read repo evidence from",
        )
    contract_path = _diagnostic_text(default_contract_path(ticket_id))
    contracts: list[tuple[ModelTicketMergedPr, dict[str, object]]] = []
    for (repo, number), pr in candidates.items():
        read = read_repo_contract(repo, pr.merge_commit_sha, ticket_id)
        if read.status is EnumRepoContractReadStatus.ABSENT:
            continue
        parsed = _repo_contract_or_refusal(
            read, ticket_id, f"{repo}#{number} at merge {pr.merge_commit_sha}"
        )
        if isinstance(parsed, ModelRepoEvidenceVerdict):
            return parsed
        contracts.append((pr, parsed))
    if not contracts:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.NOT_ENGAGED,
            detail=f"no merged product PR carries {contract_path}",
        )

    newest: dict[str, str] = {}
    for pr, _contract in contracts:
        if pr.merged_at:
            newest[pr.repo] = max(newest.get(pr.repo, ""), pr.merged_at)
    contracts = [
        (pr, contract)
        for pr, contract in contracts
        if not pr.merged_at or pr.merged_at == newest[pr.repo]
    ]

    engaged: list[ModelEngagedRepoContract] = []
    skipped: list[str] = []
    for pr, contract in contracts:
        source = f"{pr.repo}#{pr.pr_number}"
        runs = read_repo_check_runs(pr.repo, pr.head_sha)
        if runs is None:
            return ModelRepoEvidenceVerdict(
                outcome=EnumRepoEvidenceOutcome.REFUSED,
                detail=(
                    f"{_diagnostic_text(source)} at head {_diagnostic_text(pr.head_sha)}: check runs unreadable; "
                    "restore GitHub check-run access."
                ),
            )
        kept = [
            run
            for run in runs
            if run.name == REPO_EVIDENCE_CHECK_NAME
            and run.app_slug == REPO_EVIDENCE_APP_SLUG
        ]
        if not kept:
            skipped.append(
                f"{_diagnostic_text(source)} carries {contract_path} but no "
                f"{REPO_EVIDENCE_CHECK_NAME} run on head {_diagnostic_text(pr.head_sha[:12])}"
            )
            continue
        engaged.append(
            ModelEngagedRepoContract(pr=pr, contract=contract, check_runs=tuple(kept))
        )
    if not engaged:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.NOT_ENGAGED, detail="; ".join(skipped)
        )
    return tuple(engaged)


def evaluate_repo_evidence(
    ticket_id: str,
    ticket_description: str,
    merged_prs: tuple[ModelTicketMergedPr, ...],
    *,
    read_repo_contract: RepoContractReader,
    read_repo_check_runs: RepoEvidenceCheckRunsReader,
) -> ModelRepoEvidenceVerdict:
    """Decide a ticket from the product repositories its PRs merged into.

    The resolution order of the omniclaude Done gate, all I/O through the two
    readers:

    1. Read ``contracts/<ticket>.yaml`` at every merged PR's merge commit. No PR
       carrying one leaves the ticket NOT_ENGAGED (OCC decides); an unreadable
       or malformed contract REFUSES.
    2. Within one repository, only the newest merged contract-carrying PR
       decides, because its check re-verifies the whole contract. A PR without
       a merge time is never treated as superseded.
    3. A deciding PR with no ``repo-evidence / dod-verify`` run on its head has
       not adopted the repo path; when no deciding PR has one, NOT_ENGAGED.
       Unreadable check runs REFUSE.
    4. The newest run on each engaged head must be a completed success, and the
       contract at that head must equal the merged one, or REFUSED.
    5. Every labelled acceptance criterion of the ticket description must be
       bound by some engaged contract, or REFUSED. A description with no
       criterion, or an unlabelled one, REFUSES: there is nothing to bind.
    """
    resolved = resolve_repo_contracts(
        ticket_id,
        merged_prs,
        read_repo_contract=read_repo_contract,
        read_repo_check_runs=read_repo_check_runs,
    )
    if isinstance(resolved, ModelRepoEvidenceVerdict):
        return resolved

    bindings: dict[str, list[str]] = {}
    governing: list[dict[str, object]] = []
    sources: list[str] = []
    for engaged in resolved:
        pr, contract, kept = engaged.pr, engaged.contract, engaged.check_runs
        source = f"{pr.repo}#{pr.pr_number}"
        context = f"{source} at head {pr.head_sha} and merge {pr.merge_commit_sha}"
        safe_context = _diagnostic_text(context)
        # Check-run ids only grow, so the newest copy is the highest id: a rerun
        # still in progress must not lose to an older success.
        run = max(kept, key=lambda r: r.id)
        if run.status != "completed" or run.conclusion != "success":
            return ModelRepoEvidenceVerdict(
                outcome=EnumRepoEvidenceOutcome.REFUSED,
                detail=(
                    f"{safe_context}: {REPO_EVIDENCE_CHECK_NAME} run {run.id} has "
                    f"status={_diagnostic_text(run.status)} and "
                    f"conclusion={_diagnostic_text(run.conclusion)}; obtain "
                    "a completed success."
                ),
            )
        head_contract = _repo_contract_or_refusal(
            read_repo_contract(pr.repo, pr.head_sha, ticket_id), ticket_id, context
        )
        if isinstance(head_contract, ModelRepoEvidenceVerdict):
            return head_contract
        if head_contract != contract:
            return ModelRepoEvidenceVerdict(
                outcome=EnumRepoEvidenceOutcome.REFUSED,
                detail=(
                    f"{safe_context}: contract changed between the verified head and "
                    "the merge commit, so the check run does not cover what "
                    "merged; verify the merged contract in a new PR."
                ),
            )
        sources.append(f"{safe_context} ({REPO_EVIDENCE_CHECK_NAME} run {run.id})")
        governing.append(contract)
        for label, item_ids in _contract_bindings(contract).items():
            bindings.setdefault(label, []).extend(
                f"{_diagnostic_text(item_id)} ({_diagnostic_text(source)})"
                for item_id in item_ids
            )

    context = ", ".join(sources)
    criteria = acceptance_criteria_items(ticket_description)
    if not criteria:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.REFUSED,
            detail=(
                f"{context}: no acceptance criterion could be read from the "
                "ticket description, so there is nothing for the contract to "
                "bind; list labelled criteria under an `Acceptance criteria` "
                "heading."
            ),
        )
    unlabelled = [c for c in criteria if not canonical_ac_label(c)]
    if unlabelled:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.REFUSED,
            detail=(
                f"{context}: acceptance criteria carry no label and cannot be "
                f"bound by `binds_ac`: {_diagnostic_text('; '.join(unlabelled))}; label each "
                "criterion."
            ),
        )
    labels = sorted({canonical_ac_label(c) for c in criteria})
    unbound = [label for label in labels if label not in bindings]
    if unbound:
        return ModelRepoEvidenceVerdict(
            outcome=EnumRepoEvidenceOutcome.REFUSED,
            detail=(
                " | ".join(
                    f"{label}: no dod_evidence item in the contracts of {context} "
                    "binds it"
                    for label in unbound
                )
                + "; add the missing binds_ac entries."
            ),
        )
    return ModelRepoEvidenceVerdict(
        outcome=EnumRepoEvidenceOutcome.PASSED,
        detail=(
            f"repo-bound by {context}: "
            + ", ".join(f"{label}<-{sorted(bindings[label])[0]}" for label in labels)
        ),
        governing_contracts=tuple(governing),
    )


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Parse ``https://github.com/<owner>/<repo>/pull/<n>`` into ``(repo, n)``.

    Returns ``None`` for unrecognized formats. Pure function — no I/O.
    """
    match = _PR_URL_RE.match(pr_url)
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("num"))


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


def apply_supersessions(
    receipts: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Resolve the supersession chain over a raw receipt payload set (OMN-13888).

    ``_load_receipts_on_ref`` globs every ``*.yaml`` under a ticket's receipt
    directory, so the payload list mixes base ``ModelDodReceipt`` receipts with
    net-new ``ModelReceiptSupersession`` records (identified by the ``supersedes``
    key). For each receipt key ``(ticket_id, evidence_item_id, check_type)`` the
    latest supersession record decides the active receipt:

    * a tombstone record drops the base receipt (no active receipt for the key);
    * a replacement record substitutes its embedded ``replacement`` receipt.

    "Latest" is resolved by the unforgeable ``.supersede.<TOKEN>.yaml`` filename
    ordinal — the SAME authority key the omnibase_core
    :func:`resolve_supersession` resolver uses — so the two resolvers agree on
    which record wins (round-1 consistency fix). The ordinal is read from the
    ``__source_name__`` key that the :class:`ReceiptsOnRefLoader` attaches to each
    payload (the receipt's basename). The payload-carried ``created_at`` string is
    attacker-controllable, so it is used ONLY as a tiebreak among records that
    carry no filename ordinal (e.g. hand-built test payloads). Records with a
    higher token always outrank records without one.

    OMN-19121: the token orders as a dotted-numeric SEQUENCE
    (:func:`_supersede_sequence`), and the highest-ordered record then passes
    the same commit-identity guard the binding resolver applies, so a PASS
    re-filed at a prior FAIL's own ``commit_sha`` does not clear that FAIL.

    One ordering difference from the core resolver survives deliberately and is
    residual, not a defect: core sorts by ``created_at`` first and by sequence
    second, while this function keeps the filename ordinal PRIMARY because the
    payload-carried ``created_at`` reaching here is not schema-validated. The
    two can only disagree on a record filed later under a LOWER ordinal, which
    the runner's allocator cannot produce — it only ever hands out a free slot
    above the last one.

    Without this filter a tombstoned base receipt's stale citation would still be
    fed to Check 2, so an intentionally-invalidated PR (e.g. a closed-unmerged PR
    that was re-bound to the actually-merged one) would keep failing the gate.

    Pure function — no I/O.
    """

    def _key(payload: dict[str, object]) -> tuple[object, object, object]:
        return (
            payload.get("ticket_id"),
            payload.get("evidence_item_id"),
            payload.get("check_type"),
        )

    def _order_key(record: dict[str, object]) -> tuple[int, tuple[int, ...], str]:
        # Primary: the ``.supersede.<TOKEN>`` filename ordinal (unforgeable, and
        # identical to the omnibase_core resolver's authority). Records without a
        # filename ordinal sort BEFORE numbered ones (has_seq=0) so a genuine
        # numbered record always wins over a bare payload; created_at only breaks
        # ties among un-numbered records.
        #
        # OMN-19121: the ordinal is a dotted-numeric SEQUENCE, not an int, so an
        # attempt-scoped correction outranks the record it corrects. A token
        # that is not dotted-numeric keys to has_seq=0 and never compares a
        # tuple against an int.
        source_name = record.get("__source_name__")
        sequence: tuple[int, ...] | None = None
        if isinstance(source_name, str):
            match = _SUPERSEDE_SEQ_RE.search(source_name)
            if match is not None:
                sequence = _supersede_sequence(match.group(1))
        has_seq = 1 if sequence is not None else 0
        return (
            has_seq,
            sequence if sequence is not None else (),
            str(record.get("created_at", "")),
        )

    def _replacement_field(record: dict[str, object], field: str) -> str | None:
        replacement = record.get("replacement")
        if not isinstance(replacement, dict):
            return None
        value = replacement.get(field)
        return value if isinstance(value, str) else None

    def _guarded_winner(ordered: list[dict[str, object]]) -> dict[str, object]:
        """The highest-ordered record, unless it launders a prior FAIL.

        OMN-19121. Ordering alone would let any later PASS erase any earlier
        FAIL, which turns an append-only chain into a retry-until-green channel
        on the Done-flip path: re-file the same observation often enough and
        this gate stops biting. So a PASS supersedes the latest prior FAIL only
        as an INDEPENDENT OBSERVATION, meaning its replacement carries a
        different ``commit_sha`` -- something actually changed between the two
        runs. A PASS re-filed at the FAIL's own head is the same observation
        restated, and the FAIL stands.

        This gate asks the eligibility-shaped question -- which receipt decides
        whether the evidence holds -- so it takes the same guard the binding
        resolver applies. Widening the ordering WITHOUT it would have moved
        this reader from disagreeing in the closed direction to disagreeing in
        the OPEN one, which is strictly worse. The OCC hardening gate
        deliberately does not take this guard, because it asks which record
        must be REVIEWED and must never let a lower clean record mask a newer
        broken one (OMN-19111).

        Mirrors ``validator_receipt_supersession._guarded_winner``. Written out
        rather than imported for the pin reason given on ``_supersede_sequence``.
        """
        winner = ordered[-1]
        if _replacement_field(winner, "status") != "PASS":
            return winner
        winner_sha = _replacement_field(winner, "commit_sha")
        for record in reversed(ordered[:-1]):
            status = _replacement_field(record, "status")
            if status is None:
                continue
            if status != "FAIL":
                continue
            if _replacement_field(record, "commit_sha") == winner_sha:
                return record
            break
        return winner

    base: list[dict[str, object]] = []
    chains: dict[tuple[object, object, object], list[dict[str, object]]] = {}
    for payload in receipts:
        if not isinstance(payload, dict):
            continue
        if "supersedes" in payload:
            chains.setdefault(_key(payload), []).append(payload)
        else:
            base.append(payload)

    latest_by_key: dict[tuple[object, object, object], dict[str, object]] = {}
    for key, records in chains.items():
        latest_by_key[key] = _guarded_winner(sorted(records, key=_order_key))

    resolved: list[dict[str, object]] = []
    handled_keys: set[tuple[object, object, object]] = set()
    for payload in base:
        key = _key(payload)
        latest = latest_by_key.get(key)
        if latest is None:
            resolved.append(payload)
            continue
        handled_keys.add(key)
        if latest.get("tombstone"):
            continue  # key invalidated — drop the base receipt entirely
        replacement = latest.get("replacement")
        if isinstance(replacement, dict):
            resolved.append(replacement)

    # A replacement record whose base receipt was never written still re-binds.
    for key, latest in latest_by_key.items():
        if key in handled_keys or latest.get("tombstone"):
            continue
        replacement = latest.get("replacement")
        if isinstance(replacement, dict):
            resolved.append(replacement)

    return resolved


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

    OMN-16106: when no receipt names its repository by ``--repo`` or a PR URL,
    the set is read again with the GitHub REST path a receipt's own probe
    names (``repos/<owner>/<repo>/commits/<commit_sha>`` or
    ``repos/<owner>/<repo>/pulls/<pr_number>``), the only repository field an
    occ-probe-capture-v1 receipt carries. It is a fallback, so a set that
    already yields a citation reads exactly as before, and Check 2 still
    proves every citation merged in that repository.

    Pure function — no I/O.
    """
    citations = _receipt_merge_commits(receipts, _extract_receipt_repo)
    if citations:
        return citations
    return _receipt_merge_commits(receipts, _extract_receipt_repo_from_api_path)


def _receipt_merge_commits(
    receipts: list[dict[str, object]],
    resolve_repo: Callable[[dict[str, object], int], str | None],
) -> list[ModelCitedMergeCommit]:
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
        repo = resolve_repo(receipt, pr_number)
        if repo is None:
            continue
        key = (repo, pr_number, sha)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            ModelCitedMergeCommit(
                pr_url=f"{service_endpoint_url('github.web_url')}/{repo}/pull/{pr_number}",
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


_GITHUB_API_REPO_PATH_RE = re.compile(
    r"(?:^|[\s\"'/])repos/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/"
    r"(?:commits/(?P<sha>[0-9a-fA-F]{7,40})|pulls/(?P<pr>\d+))(?![0-9A-Za-z])"
)


def _extract_receipt_repo_from_api_path(
    receipt: dict[str, object], pr_number: int
) -> str | None:
    """Resolve ``owner/repo`` from a GitHub REST path naming this receipt.

    A path counts only when it names the receipt's own ``commit_sha`` or its
    own ``pr_number``; a probe of any other commit or PR names nothing.
    """
    sha = receipt.get("commit_sha")
    if not isinstance(sha, str):
        return None
    for field in ("probe_command", "check_value"):
        value = receipt.get(field)
        if not isinstance(value, str):
            continue
        for match in _GITHUB_API_REPO_PATH_RE.finditer(value):
            probed_sha = match.group("sha")
            probed_pr = match.group("pr")
            if probed_sha is not None and _sha_prefix_match(probed_sha, sha):
                return match.group("repo")
            if probed_pr is not None and int(probed_pr) == pr_number:
                return match.group("repo")
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
        re.escape(service_endpoint_url("github.web_url"))
        + r"/[^\s\"')]+/[^\s\"')]+/pull/\d+(?:/[^\s\"')]*)?",
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

    Construction takes four Protocol-typed probes (released tags, release
    files, and the two product-repository readers) so unit tests can inject
    deterministic stubs. Production wiring uses the GitHub-backed
    implementations of :class:`HandlerDurableEvidenceGateEffect`. A ticket no
    product repository governs is refused (OMN-20074).
    """

    def __init__(
        self,
        *,
        release_tags_containing: ReleaseTagsContainingProbe,
        index_release_files: IndexReleaseFilesProbe,
        read_repo_contract: RepoContractReader,
        read_repo_check_runs: RepoEvidenceCheckRunsReader,
    ) -> None:
        # OMN-18010: the two released-is-Done probes are REQUIRED constructor
        # arguments, not optional ones with a permissive default. An optional
        # probe that silently skips the check when unconfigured is the
        # detection-not-enforcement anti-pattern (CLAUDE.md rule 5) and would
        # reproduce, in this gate, the exact failure it exists to close.
        self._release_tags_containing = release_tags_containing
        self._index_release_files = index_release_files
        # OMN-20074: the product-repository readers are required too. With no
        # reader no repo evidence can be read, and a ticket no product
        # repository governs is refused, never decided on a fallback.
        self._read_repo_contract = read_repo_contract
        self._read_repo_check_runs = read_repo_check_runs

    def evaluate(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str] = frozenset(),
        merged_prs: tuple[ModelTicketMergedPr, ...] = (),
        ticket_description: str = "",
        verdict_criteria_revision: str | None = None,
    ) -> ModelDurableEvidenceGateResult:
        """Run the durable-evidence checks, then re-check the verdict's criteria revision.

        ``verdict_criteria_revision`` is the ``criteria_revision`` the dod_verify
        verdict carried (OMN-20858). When given, the live ``ticket_description``
        must still have that revision: a criterion edited, deleted or added after
        the verdict was computed makes the verdict stale, and the Done transition
        refuses it. An empty description cannot be compared and refuses too, never
        passing as unchanged. Omitted, the gate behaves exactly as before.

        Pure result — does not raise. Callers that want hard-fail semantics
        invoke :meth:`enforce` instead.
        """
        result = self._evaluate_durable(
            ticket_id=ticket_id,
            contract=contract,
            ticket_labels=ticket_labels,
            merged_prs=merged_prs,
            ticket_description=ticket_description,
        )

        if verdict_criteria_revision is None:
            return result
        live_revision = (
            criteria_revision(ticket_description) if ticket_description else None
        )
        current = live_revision == verdict_criteria_revision
        if current:
            message = (
                f"The verdict's criteria revision {verdict_criteria_revision[:12]} "
                f"is still the live revision of {ticket_id}."
            )
        elif live_revision is None:
            message = (
                f"CRITERIA_DRIFT: the verdict for {ticket_id} was computed against "
                f"criteria revision {verdict_criteria_revision[:12]}, and the live "
                "ticket description was not supplied, so the revision cannot be "
                "re-checked. A revision that cannot be re-checked is not current; "
                "re-run dod_verify against the ticket."
            )
        else:
            message = (
                f"CRITERIA_DRIFT: the verdict for {ticket_id} was computed against "
                f"criteria revision {verdict_criteria_revision[:12]} and the live "
                f"ticket is at {live_revision[:12]}: a criterion was edited, "
                "deleted or added since. Re-run dod_verify, and amend and "
                "re-accept the contract bindings it names."
            )
        check = ModelDurableEvidenceCheckResult(
            check=EnumDurableEvidenceCheck.CRITERIA_REVISION_CURRENT,
            passed=current,
            message=message,
        )
        return ModelDurableEvidenceGateResult(
            ticket_id=result.ticket_id,
            status=(result.status if current else EnumDurableEvidenceStatus.FAIL),
            checks=[*result.checks, check],
        )

    def _evaluate_durable(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str] = frozenset(),
        merged_prs: tuple[ModelTicketMergedPr, ...] = (),
        ticket_description: str = "",
    ) -> ModelDurableEvidenceGateResult:
        """Decide a ticket on its product-repository contract, or refuse it.

        Args:
            ticket_id: The Linear ticket ID (e.g. ``OMN-9855``).
            contract: The parsed contract dict the node verified — already
                validated by the EvidenceCollector load path.
            merged_prs: The ticket's merged PRs. Their repositories are read
                for the ticket's contract (OMN-20071).
            ticket_description: The ticket description whose labelled
                acceptance criteria a repo contract must bind.

        A ticket no product repository governs (the repo verdict is
        NOT_ENGAGED) is refused with the reason the repository read gave; no
        other source is read in its place (OMN-20074). Pure result — does not
        raise. Callers that want hard-fail semantics invoke :meth:`enforce`
        instead.
        """
        repo_verdict = evaluate_repo_evidence(
            ticket_id,
            ticket_description,
            merged_prs,
            read_repo_contract=self._read_repo_contract,
            read_repo_check_runs=self._read_repo_check_runs,
        )
        if repo_verdict.outcome is EnumRepoEvidenceOutcome.NOT_ENGAGED:
            repo_verdict = ModelRepoEvidenceVerdict(
                outcome=EnumRepoEvidenceOutcome.REFUSED,
                detail=(
                    f"no product repository governs {ticket_id}: {repo_verdict.detail}."
                ),
            )
        return self._evaluate_repo_governed(
            ticket_id=ticket_id,
            contract=contract,
            ticket_labels=ticket_labels,
            merged_prs=merged_prs,
            verdict=repo_verdict,
        )

    def _evaluate_repo_governed(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str],
        merged_prs: tuple[ModelTicketMergedPr, ...],
        verdict: ModelRepoEvidenceVerdict,
    ) -> ModelDurableEvidenceGateResult:
        """Decide a ticket on its product-repository contract.

        The durable record is the merged contract and the check run on the
        merged head, both of which :func:`evaluate_repo_evidence` read. Check 3
        carries that verdict, a refusal included. The caller's ``contract`` is
        not compared against the merged one. Released, repair-to-ratchet and
        done-class keep their meaning, the released check over every merged PR
        of a publishing repository and repair-to-ratchet over the governing
        contracts.
        """
        not_applicable = (
            f"Not applicable: {ticket_id} is decided on its product-repository "
            "contract, which no receipt trail is read beside; "
            f"{EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN.value} carries the "
            "verdict."
        )
        checks = [
            ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.RECEIPT_TRACKED,
                passed=True,
                message=not_applicable,
            ),
            ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
                passed=True,
                message=not_applicable,
            ),
            self._released_check(
                tuple(
                    ModelReleasedCitationInput(
                        repo=pr.repo,
                        pr_number=pr.pr_number,
                        merge_sha=pr.merge_commit_sha,
                    )
                    for pr in merged_prs
                    if is_publishing_repo(pr.repo)
                )
            ),
        ]

        governed = verdict.outcome is EnumRepoEvidenceOutcome.PASSED
        message = (
            f"Product-repository contract governs: {verdict.detail}."
            if governed
            else (
                f"Product-repository contract refused: {verdict.detail} "
                "Transitioning Linear to Done needs the product-repository "
                "evidence fixed; no other source is read in its place."
            )
        )
        checks.append(
            ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
                passed=governed,
                message=message,
            )
        )
        # The merged repo contract is the one that governs, so repair-to-ratchet
        # reads its prevention fields; the caller's copy (possibly an OCC one)
        # is used only when no repo contract governed.
        defect_contract = next(
            (
                governing_contract
                for governing_contract in verdict.governing_contracts
                if any(
                    field is not None
                    for field in extract_defect_prevention(governing_contract)
                )
            ),
            verdict.governing_contracts[0] if verdict.governing_contracts else contract,
        )
        checks.append(self._defect_prevention_check(defect_contract, ticket_labels))
        checks.append(
            self._done_class_check(
                ticket_labels,
                backed=governed,
                unbacked=(
                    "but the product-repository contract does not govern the "
                    "ticket (contract_on_occ_main failed). A done-class label "
                    "must be backed by durable evidence — the label alone is "
                    "not evidence (OMN-13337)."
                ),
                backed_by=(
                    "backed by the product-repository contract and its green "
                    f"{REPO_EVIDENCE_CHECK_NAME} run."
                ),
            )
        )
        return ModelDurableEvidenceGateResult(
            ticket_id=ticket_id,
            status=(
                EnumDurableEvidenceStatus.PASS
                if all(c.passed for c in checks)
                else EnumDurableEvidenceStatus.FAIL
            ),
            checks=checks,
        )

    def _released_check(
        self, released_inputs: tuple[ModelReleasedCitationInput, ...]
    ) -> ModelDurableEvidenceCheckResult:
        """Check 2b (OMN-18010): every merge in a publishing repo is released."""
        released_result = evaluate_released(
            released_inputs,
            release_tags_containing=self._release_tags_containing,
            index_release_files=self._index_release_files,
        )
        return ModelDurableEvidenceCheckResult(
            check=EnumDurableEvidenceCheck.RELEASED_ON_PUBLISHING_REPO,
            passed=released_result.passed,
            message=(
                released_result.message
                if released_result.outcome is not EnumReleasedOutcome.NOT_APPLICABLE
                else (
                    "No merged evidence PR lands in a publishing repo, so the "
                    "released-is-Done check does not apply."
                )
            ),
        )

    @staticmethod
    def _defect_prevention_check(
        contract: dict[str, object], ticket_labels: frozenset[str]
    ) -> ModelDurableEvidenceCheckResult:
        """Check 4 (OMN-13339): a defect ticket links a prevention gate or note."""
        defect_labels = EnumDefectLabel.values()
        present_defect_labels = sorted(ticket_labels & defect_labels)
        if not present_defect_labels:
            return ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
                passed=True,
                message=(
                    "Not a defect-class ticket — repair-to-ratchet rule does not apply."
                ),
            )
        prevention_gate, non_recurrence_note = extract_defect_prevention(contract)
        if prevention_gate is not None or non_recurrence_note is not None:
            satisfied_by = (
                f"prevention_gate={prevention_gate!r}"
                if prevention_gate is not None
                else f"non_recurrence_note={non_recurrence_note!r}"
            )
            return ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
                passed=True,
                message=(
                    f"Defect ticket (label(s) {present_defect_labels}) "
                    f"satisfies repair-to-ratchet via {satisfied_by}."
                ),
            )
        return ModelDurableEvidenceCheckResult(
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

    @staticmethod
    def _done_class_check(
        ticket_labels: frozenset[str],
        *,
        backed: bool,
        unbacked: str,
        backed_by: str,
    ) -> ModelDurableEvidenceCheckResult:
        """Check 4 (OMN-13337): an approved done-class label backed by evidence."""
        approved_labels = EnumDoneClassLabel.values()
        present_done_classes = sorted(ticket_labels & approved_labels)
        if not present_done_classes:
            return ModelDurableEvidenceCheckResult(
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
        if not backed:
            return ModelDurableEvidenceCheckResult(
                check=EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
                passed=False,
                message=(
                    f"Ticket carries done-class label(s) {present_done_classes} "
                    f"{unbacked}"
                ),
            )
        return ModelDurableEvidenceCheckResult(
            check=EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
            passed=True,
            message=(
                f"Done-class label(s) {present_done_classes} present and {backed_by}"
            ),
        )

    def enforce(
        self,
        *,
        ticket_id: str,
        contract: dict[str, object],
        ticket_labels: frozenset[str] = frozenset(),
        merged_prs: tuple[ModelTicketMergedPr, ...] = (),
        ticket_description: str = "",
        verdict_criteria_revision: str | None = None,
    ) -> ModelDurableEvidenceGateResult:
        """Run :meth:`evaluate` and raise on failure.

        On failure raises :class:`DurableEvidenceGateError` carrying the
        structured result. On success returns the result.
        """
        result = self.evaluate(
            ticket_id=ticket_id,
            contract=contract,
            ticket_labels=ticket_labels,
            merged_prs=merged_prs,
            ticket_description=ticket_description,
            verdict_criteria_revision=verdict_criteria_revision,
        )
        if result.status != EnumDurableEvidenceStatus.PASS:
            raise DurableEvidenceGateError(result)
        return result


__all__: list[str] = [
    "REPO_EVIDENCE_APP_SLUG",
    "REPO_EVIDENCE_CHECK_NAME",
    "DurableEvidenceGate",
    "DurableEvidenceGateError",
    "IndexReleaseFilesProbe",
    "ReleaseTagsContainingProbe",
    "RepoContractReader",
    "RepoEvidenceCheckRunsReader",
    "apply_supersessions",
    "default_contract_path",
    "evaluate_repo_evidence",
    "extract_receipt_merge_commits",
    "parse_pr_url",
    "resolve_repo_contracts",
]
