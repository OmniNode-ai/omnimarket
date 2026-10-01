# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-mint an OCC companion that a sibling companion put into conflict (OMN-18856).

Why this module exists
----------------------
Every per-PR companion edits the ticket's shared ``contracts/<ticket>.yaml``:
it appends its own ``dod_evidence`` entries at the end of the list, or creates
the file when it is the ticket's first. Two companions for the same ticket
therefore both write the same place in the same file, and when one of them
lands on onex_change_control ``dev`` the other becomes a content conflict.
Measured 2026-09-26: 13 open companions in that state, 204 PR-hours, median
6.5h each, every one conflicting ONLY in its ``contracts/*.yaml``.

The producer already knows how to repair this. ``OccCompanionEmitter``
(OMN-18856, ``omnimarket#2734``) re-mints a companion that is bound to its own
product PR and is OPEN and un-mergeable: it clones a fresh ``dev``, re-renders
the contract from the contract now on ``dev`` plus this PR's entries, and
force-pushes the same deterministic branch, so the companion PR number and the
product PR's evidence line never change. Proven live on 2026-09-26T22:46Z:
one replay of the ordinary autobind publisher for ``omniclaude#2367`` moved
OCC#11425 from ``d9face0dd9`` (conflicting) to ``c4b0553785`` (clean, 109
added lines, no line removed).

What is missing is the trigger. The emitter runs only on a product PR
lifecycle event, and the event that creates the conflict -- a sibling
companion merging -- happens in onex_change_control and fires nothing in the
product repo. Every one of the 12 conflicted companions measured had its last
product event BEFORE the sibling landed, so nothing ever asked the emitter.

What this does
--------------
On a schedule, for each open machine-minted per-PR companion, it answers one
question: *did a later ``dev`` commit to this companion's contract put it into
conflict?* When the answer is yes, it publishes the ordinary
``onex.cmd.omnimarket.occ-autobind.v1`` command for that companion's product PR
through the canonical ``scripts/publish_occ_autobind_command.py``. The command
is byte-identical to the one a product PR push publishes. The emitter then
applies every one of its own guards (closed, draft, hold markers, identity,
the lease, append-only) before it writes anything.

Nothing here writes to onex_change_control, edits a contract, touches a gate or
decides what evidence is required. It only supplies the missing trigger.

The second half -- executed receipts
------------------------------------
A re-mint rebuilds the branch from ``dev`` and so drops the executed
``test_passes`` receipts that ``occ-receipt-runner.yml`` (OMN-16859) had pushed
onto the old branch, leaving the emitter's PENDING base receipt. That runner
only fires on a product PR event too. For companions whose product PR lives in
THIS repository (the only repository that has the runner), this module also
dispatches the runner once per companion head when a PENDING base receipt has
no executed record beside it. The runner itself decides PASS or FAIL.

Why a schedule, and why here
----------------------------
Dispatching from onex_change_control into a product repo needs ``actions:
write`` there, and no identity the org holds has it (OMN-18797). Publishing the
command needs only the bus credentials this repository's own autobind publisher
already uses, and omnimarket owns the canonical publisher script, so one
central schedule here covers every product repository without a new
credential and without a copy in each repository.

Loop bounds
-----------
A companion is re-minted only when its head commit is OLDER than the newest
``dev`` commit to one of its contracts. A successful re-mint makes the head
newer, so the same conflict can never trigger twice; a companion that is still
conflicting after a re-mint is reported as ``stuck_after_remint`` and left for
a person, never retried in a loop. The receipt runner is dispatched at most
once per companion head, keyed on the run name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # fixed argv, no shell, trusted binaries
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1

OCC_REPO_DEFAULT: Final[str] = "OmniNode-ai/onex_change_control"
OCC_BASE_BRANCH: Final[str] = "dev"
MACHINE_MINTED_LABEL: Final[str] = "occ:machine-minted"
RECEIPT_RUNNER_WORKFLOW: Final[str] = "occ-receipt-runner.yml"

#: The receipt runner's dispatched run name. Must match ``run-name`` in
#: ``.github/workflows/occ-receipt-runner.yml`` exactly; it is how this module
#: tells whether the runner already ran for a companion head.
RECEIPT_RUNNER_RUN_NAME: Final[str] = "OCC Receipt Runner PR #{pr}"

#: How long a companion head must have existed before the receipt runner is
#: dispatched for it. The event-driven runner normally fires on the same push
#: that minted the head; this leaves it room to do so first.
RECEIPT_RUNNER_GRACE: Final[timedelta] = timedelta(minutes=5)

#: The publisher requires a head sha for its log line only; the command payload
#: carries repo, PR number and ticket. A private product repo cannot be read
#: with this repository's token, so its head is recorded as unread rather than
#: guessed.
UNREAD_HEAD_SHA: Final[str] = "unread-private-repo"

#: How long a ready, unbound member's head must have stood before the schedule
#: re-drives it (OMN-17427). The event-driven publish and the mint-verify retry
#: (``occ-autobind-mint-verify.yml``, about 150 seconds after the publish) go
#: first; this only catches the member they left unbound.
WINDOW_REDRIVE_GRACE: Final[timedelta] = timedelta(minutes=15)

_OPEN_PR_LIMIT: Final[int] = 1000
_MERGEABILITY_READS: Final[int] = 6
_MERGEABILITY_WAIT_SECONDS: Final[float] = 5.0

#: ``auto/<owner>-<repo>-pr-<n>-occ-autobind``; mirrors
#: ``omnimarket.events.occ_companion.companion_branch_for``. The per-ticket
#: batch branch ``auto/ticket-omn-<n>-occ-autobind`` does not match: it has no
#: single product PR to replay.
_PER_PR_BRANCH_RE: Final = re.compile(
    r"^auto/(?P<slug>.+)-pr-(?P<pr>\d+)-occ-autobind$"
)
#: Both batch shapes (OMN-16336): the per-ticket branch and the per-repo batch
#: window ``auto/window-<owner>-<repo>-occ-autobind``. Neither has a single
#: product PR to replay.
_BATCH_BRANCH_RE: Final = re.compile(
    r"^auto/(?:ticket-omn-\d+|window-[a-z0-9_.-]+)-occ-autobind$"
)
#: Both producers title a companion "... for <Owner>/<repo>#<n>".
_TITLE_TARGET_RE: Final = re.compile(
    r"\bfor (?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<pr>\d+)\b"
)
_TITLE_TICKET_RE: Final = re.compile(r"\b(OMN-\d+)\b")
_EVIDENCE_SOURCE_RE: Final = re.compile(
    r"^\s*Evidence-Source:\s*OCC#(?P<n>\d+)\s*$", re.IGNORECASE | re.MULTILINE
)
_PENDING_BASE_RECEIPT_RE: Final = re.compile(
    r"^(?P<dir>drift/dod_receipts/.+)/test_passes\.yaml$"
)


def companion_branch_for(repo: str, pr_number: int) -> str:
    """The deterministic per-PR companion branch; same rule as the producers."""
    return f"auto/{repo.replace('/', '-').lower()}-pr-{pr_number}-occ-autobind"


class EnumRemintOutcome(StrEnum):
    """Every answer :func:`decide_remint` can give. Only ``REMINT`` acts."""

    REMINT = "remint"
    NOT_MACHINE_MINTED = "not_machine_minted"
    DRAFT = "draft"
    BATCH_BRANCH = "batch_branch"
    NOT_PER_PR_BRANCH = "not_per_pr_branch"
    IDENTITY_MISMATCH = "identity_mismatch"
    MERGEABLE = "mergeable"
    MERGEABILITY_UNKNOWN = "mergeability_unknown"
    NOT_DIRTY = "not_dirty"
    NO_CONTRACT = "no_contract"
    NO_SIBLING_COMMIT = "no_sibling_commit"
    STUCK_AFTER_REMINT = "stuck_after_remint"
    PRODUCT_NOT_OPEN = "product_not_open"
    PRODUCT_DRAFT = "product_draft"
    PRODUCT_UNBOUND = "product_unbound"


class EnumReceiptOutcome(StrEnum):
    """Every answer :func:`decide_receipt_refire`` can give. Only ``DISPATCH`` acts."""

    DISPATCH = "dispatch"
    OTHER_REPO = "other_repo"
    NOT_MERGEABLE = "not_mergeable"
    NO_PENDING_RECEIPT = "no_pending_receipt"
    TOO_FRESH = "too_fresh"
    ALREADY_RAN = "already_ran"
    PRODUCT_NOT_READY = "product_not_ready"


class EnumRedriveOutcome(StrEnum):
    """Every answer :func:`decide_window_redrive` can give. Only ``REDRIVE`` acts."""

    REDRIVE = "redrive"
    DRAFT = "draft"
    BOUND = "bound"
    NO_TICKET = "no_ticket"
    TOO_FRESH = "too_fresh"
    WINDOW_OPEN = "window_open"


@dataclass(frozen=True)
class MemberFacts:
    """One open product PR of this repository, as the window re-drive reads it.

    ``head_committed_at`` is read only for a ready, unbound PR; ``None`` elsewhere.
    """

    number: int
    title: str
    body: str
    draft: bool
    head_sha: str
    head_committed_at: datetime | None = None


@dataclass(frozen=True)
class RedriveDecision:
    outcome: EnumRedriveOutcome
    reason: str
    target: Target | None = None


@dataclass(frozen=True)
class CompanionFacts:
    """What the decision needs to know about one open OCC companion PR."""

    number: int
    title: str
    head_ref: str
    head_sha: str
    draft: bool
    labels: tuple[str, ...]
    mergeable: bool | None
    mergeable_state: str
    head_committed_at: datetime
    files: tuple[str, ...]

    @property
    def contract_paths(self) -> tuple[str, ...]:
        return tuple(
            f for f in self.files if f.startswith("contracts/") and f.endswith(".yaml")
        )


@dataclass(frozen=True)
class ProductFacts:
    """The companion's product PR, or ``readable=False`` when the token cannot see it."""

    readable: bool
    state: str = ""
    merged: bool = False
    draft: bool = False
    body: str = ""
    head_sha: str = ""
    title: str = ""


@dataclass(frozen=True)
class Target:
    """The product PR a companion was minted for, read from its title and branch."""

    repo: str
    pr_number: int
    ticket: str


@dataclass(frozen=True)
class RemintDecision:
    outcome: EnumRemintOutcome
    reason: str
    target: Target | None = None


@dataclass(frozen=True)
class ReceiptDecision:
    outcome: EnumReceiptOutcome
    reason: str


def resolve_target(companion: CompanionFacts) -> Target | RemintDecision:
    """The product PR this companion belongs to, or the refusal that says why not.

    The title names the product PR in its exact case; the branch is the key the
    producers opened the companion under. Both must agree, so a companion whose
    title and branch name different PRs is never replayed for either.
    """
    if _BATCH_BRANCH_RE.fullmatch(companion.head_ref):
        return RemintDecision(
            EnumRemintOutcome.BATCH_BRANCH,
            f"{companion.head_ref} is a batch companion branch; it has no single "
            "product PR to replay",
        )
    if not _PER_PR_BRANCH_RE.fullmatch(companion.head_ref):
        return RemintDecision(
            EnumRemintOutcome.NOT_PER_PR_BRANCH,
            f"{companion.head_ref} is not a per-PR autobind branch",
        )
    match = _TITLE_TARGET_RE.search(companion.title)
    if match is None:
        return RemintDecision(
            EnumRemintOutcome.IDENTITY_MISMATCH,
            f"title {companion.title!r} names no <owner>/<repo>#<n> target",
        )
    repo = match.group("repo")
    pr_number = int(match.group("pr"))
    expected = companion_branch_for(repo, pr_number)
    if expected != companion.head_ref:
        return RemintDecision(
            EnumRemintOutcome.IDENTITY_MISMATCH,
            f"title names {repo}#{pr_number} (branch {expected}) but the companion "
            f"is on {companion.head_ref}",
        )
    ticket_match = _TITLE_TICKET_RE.search(companion.title)
    ticket = ticket_match.group(1) if ticket_match else ""
    return Target(repo=repo, pr_number=pr_number, ticket=ticket)


def _product_refusal(
    companion: CompanionFacts, target: Target, product: ProductFacts
) -> RemintDecision | None:
    """Refuse when the product PR cannot take a replay; ``None`` when it can.

    An unreadable product PR is not refused here: the emitter reads it with its
    own credential and applies the same checks before writing anything.
    """
    if not product.readable:
        return None
    if product.merged or product.state.lower() != "open":
        return RemintDecision(
            EnumRemintOutcome.PRODUCT_NOT_OPEN,
            f"{target.repo}#{target.pr_number} is "
            f"{'merged' if product.merged else product.state}; the emitter refuses "
            "a replay for a non-open PR",
            target,
        )
    if product.draft:
        return RemintDecision(
            EnumRemintOutcome.PRODUCT_DRAFT,
            f"{target.repo}#{target.pr_number} is a draft; the emitter suppresses it",
            target,
        )
    cited = [int(m.group("n")) for m in _EVIDENCE_SOURCE_RE.finditer(product.body)]
    if cited != [companion.number]:
        return RemintDecision(
            EnumRemintOutcome.PRODUCT_UNBOUND,
            f"{target.repo}#{target.pr_number} cites {cited or 'no'} evidence "
            f"source(s), not exactly OCC#{companion.number}; the emitter would not "
            "re-mint this companion from that body",
            target,
        )
    return None


def decide_remint(
    companion: CompanionFacts,
    newest_sibling_commit_at: datetime | None,
    product: ProductFacts,
) -> RemintDecision:
    """Whether a later ``dev`` commit to this companion's contract conflicted it.

    Pure. ``newest_sibling_commit_at`` is the newest commit on the OCC base
    branch that touched one of the companion's ``contracts/*.yaml`` files after
    the companion's own head commit, or ``None`` when there is none.
    """
    if MACHINE_MINTED_LABEL not in companion.labels:
        return RemintDecision(
            EnumRemintOutcome.NOT_MACHINE_MINTED,
            f"OCC#{companion.number} lacks {MACHINE_MINTED_LABEL}; only a producer-"
            "minted companion is re-minted by its producer",
        )
    if companion.draft:
        return RemintDecision(
            EnumRemintOutcome.DRAFT, f"OCC#{companion.number} is a draft"
        )
    resolved = resolve_target(companion)
    if isinstance(resolved, RemintDecision):
        return resolved
    target = resolved
    if companion.mergeable is None:
        return RemintDecision(
            EnumRemintOutcome.MERGEABILITY_UNKNOWN,
            f"GitHub has not computed OCC#{companion.number}'s mergeability; the "
            "next pass reads it again",
            target,
        )
    if companion.mergeable:
        return RemintDecision(
            EnumRemintOutcome.MERGEABLE,
            f"OCC#{companion.number} merges cleanly",
            target,
        )
    if companion.mergeable_state != "dirty":
        return RemintDecision(
            EnumRemintOutcome.NOT_DIRTY,
            f"OCC#{companion.number} is unmergeable but mergeable_state is "
            f"{companion.mergeable_state!r}, not a content conflict",
            target,
        )
    if not companion.contract_paths:
        return RemintDecision(
            EnumRemintOutcome.NO_CONTRACT,
            f"OCC#{companion.number} conflicts but edits no contracts/*.yaml, so it "
            "is not the sibling-contract class this module repairs",
            target,
        )
    if newest_sibling_commit_at is None:
        return RemintDecision(
            EnumRemintOutcome.NO_SIBLING_COMMIT,
            f"OCC#{companion.number} conflicts but no {OCC_BASE_BRANCH} commit touched "
            f"{', '.join(companion.contract_paths)} after its head commit",
            target,
        )
    if companion.head_committed_at >= newest_sibling_commit_at:
        return RemintDecision(
            EnumRemintOutcome.STUCK_AFTER_REMINT,
            f"OCC#{companion.number}'s head ({companion.head_committed_at.isoformat()}) "
            "is already newer than the sibling commit that conflicted it "
            f"({newest_sibling_commit_at.isoformat()}) and it still conflicts; a "
            "re-mint did not clear it, so this needs a person, not another re-mint",
            target,
        )
    refusal = _product_refusal(companion, target, product)
    if refusal is not None:
        return refusal
    return RemintDecision(
        EnumRemintOutcome.REMINT,
        f"a {OCC_BASE_BRANCH} commit at {newest_sibling_commit_at.isoformat()} to "
        f"{', '.join(companion.contract_paths)} conflicted OCC#{companion.number} "
        f"(head {companion.head_committed_at.isoformat()}); replaying autobind for "
        f"{target.repo}#{target.pr_number}",
        target,
    )


def pending_receipt_dirs(files: tuple[str, ...]) -> tuple[str, ...]:
    """Receipt directories holding a base ``test_passes.yaml`` with no executed record.

    The emitter mints the base receipt PENDING because it has no product checkout;
    the receipt runner adds ``test_passes.supersede.<pr>.yaml`` beside it once it
    has executed the check. A base receipt with no supersede record in the same
    companion is the state a re-mint leaves behind.
    """
    fileset = set(files)
    pending: list[str] = []
    for path in files:
        match = _PENDING_BASE_RECEIPT_RE.match(path)
        if match is None:
            continue
        directory = match.group("dir")
        executed = any(
            f.startswith(f"{directory}/test_passes.supersede.") for f in fileset
        )
        if not executed:
            pending.append(directory)
    return tuple(sorted(pending))


def decide_receipt_refire(
    companion: CompanionFacts,
    target: Target | None,
    product: ProductFacts,
    *,
    this_repo: str,
    runner_ran_since_head: bool,
    now: datetime,
) -> ReceiptDecision:
    """Whether to dispatch this repo's receipt runner for the companion's product PR."""
    if target is None or target.repo.lower() != this_repo.lower():
        return ReceiptDecision(
            EnumReceiptOutcome.OTHER_REPO,
            "the receipt runner exists only in this repository",
        )
    if companion.mergeable is not True:
        return ReceiptDecision(
            EnumReceiptOutcome.NOT_MERGEABLE,
            f"OCC#{companion.number} is not known to merge cleanly yet; receipts "
            "wait for the re-mint",
        )
    pending = pending_receipt_dirs(companion.files)
    if not pending:
        return ReceiptDecision(
            EnumReceiptOutcome.NO_PENDING_RECEIPT,
            f"OCC#{companion.number} carries no unexecuted test_passes receipt",
        )
    if now - companion.head_committed_at < RECEIPT_RUNNER_GRACE:
        return ReceiptDecision(
            EnumReceiptOutcome.TOO_FRESH,
            f"OCC#{companion.number}'s head is younger than "
            f"{int(RECEIPT_RUNNER_GRACE.total_seconds() // 60)} minutes; the "
            "event-driven runner goes first",
        )
    if runner_ran_since_head:
        return ReceiptDecision(
            EnumReceiptOutcome.ALREADY_RAN,
            f"the receipt runner already ran for {target.repo}#{target.pr_number} "
            f"after OCC#{companion.number}'s head; its verdict stands",
        )
    if not product.readable or product.state.lower() != "open" or product.draft:
        return ReceiptDecision(
            EnumReceiptOutcome.PRODUCT_NOT_READY,
            f"{target.repo}#{target.pr_number} is not an open, ready PR",
        )
    return ReceiptDecision(
        EnumReceiptOutcome.DISPATCH,
        f"OCC#{companion.number} holds unexecuted receipts in {', '.join(pending)}; "
        f"dispatching the receipt runner for {target.repo}#{target.pr_number}",
    )


def window_companion_branch_for(repo: str) -> str:
    """The repository's batch-window branch; mirrors ``occ_companion.window_companion_branch_for``."""
    return f"auto/window-{repo.replace('/', '-').lower()}-occ-autobind"


def decide_window_redrive(
    member: MemberFacts,
    *,
    repo: str,
    open_window: int | None,
    now: datetime,
) -> RedriveDecision:
    """Whether to republish the window-mode autobind command for one member (OMN-17427).

    Pure. The emitter skips a member that arrives while its repository's window
    is in flight (``skip:WINDOW_IN_FLIGHT``, OMN-20042) and says it binds on the
    next rebuild, but nothing publishes for that member again once the window
    merges: the product PR fires no event, and mint-verify's one retry lands
    inside the same hold. The member then stays unbound until a person
    hand-authors a companion. This supplies that missing trigger, only while no
    window is open, so it never pushes into an in-flight window.
    """
    if member.draft:
        return RedriveDecision(
            EnumRedriveOutcome.DRAFT, f"{repo}#{member.number} is a draft"
        )
    cited = [int(m.group("n")) for m in _EVIDENCE_SOURCE_RE.finditer(member.body)]
    if cited:
        return RedriveDecision(
            EnumRedriveOutcome.BOUND,
            f"{repo}#{member.number} already cites OCC#{cited[0]}",
        )
    ticket_match = _TITLE_TICKET_RE.search(member.title)
    if ticket_match is None:
        return RedriveDecision(
            EnumRedriveOutcome.NO_TICKET,
            f"{repo}#{member.number}'s title cites no OMN ticket",
        )
    target = Target(repo=repo, pr_number=member.number, ticket=ticket_match.group(1))
    if (
        member.head_committed_at is None
        or now - member.head_committed_at < WINDOW_REDRIVE_GRACE
    ):
        return RedriveDecision(
            EnumRedriveOutcome.TOO_FRESH,
            f"{repo}#{member.number}'s head is younger than "
            f"{int(WINDOW_REDRIVE_GRACE.total_seconds() // 60)} minutes; the "
            "event-driven publish goes first",
            target,
        )
    if open_window is not None:
        return RedriveDecision(
            EnumRedriveOutcome.WINDOW_OPEN,
            f"{repo}'s window OCC#{open_window} is open; {repo}#{member.number} is "
            "re-driven on the first pass after it merges",
            target,
        )
    return RedriveDecision(
        EnumRedriveOutcome.REDRIVE,
        f"{repo}#{member.number} is ready and unbound with no open window; "
        "republishing its window autobind command",
        target,
    )


class GhPort(Protocol):
    """The live reads and the two writes; faked in tests."""

    def open_companion_numbers(self, *, occ_repo: str) -> tuple[int, ...]: ...

    def companion(self, *, occ_repo: str, number: int) -> CompanionFacts: ...

    def newest_commit_since(
        self, *, occ_repo: str, path: str, since: datetime
    ) -> datetime | None: ...

    def product(self, *, repo: str, pr_number: int) -> ProductFacts: ...

    def receipt_runner_ran_since(
        self, *, repo: str, pr_number: int, since: datetime
    ) -> bool: ...

    def publish_autobind(
        self, *, target: Target, product: ProductFacts, lane: str
    ) -> str: ...

    def dispatch_receipt_runner(self, *, repo: str, pr_number: int) -> None: ...


class RedrivePort(Protocol):
    """The window re-drive's reads and its one write; faked in tests."""

    def open_members(self, *, repo: str) -> tuple[MemberFacts, ...]: ...

    def head_committed_at(self, *, repo: str, sha: str) -> datetime: ...

    def open_window_number(self, *, occ_repo: str, branch: str) -> int | None: ...

    def publish_window_autobind(
        self, *, target: Target, member: MemberFacts, lane: str
    ) -> str: ...


def _obj(value: object) -> dict[str, object]:
    """A JSON object, or an empty one; the caller decides what a missing key means."""
    return value if isinstance(value, dict) else {}


def _parse_ts(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"expected an ISO-8601 timestamp, got {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class GhCli:
    """:class:`GhPort` over the ``gh`` binary. Fixed argv, never a shell."""

    def __init__(self, publisher: Path) -> None:
        self._publisher = publisher
        self._sleep = time.sleep

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )

    def _json(self, args: list[str]) -> object:
        completed = self._run(args)
        if completed.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        try:
            return json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh {' '.join(args)} returned non-JSON: {exc}") from exc

    def open_companion_numbers(self, *, occ_repo: str) -> tuple[int, ...]:
        payload = self._json(
            [
                "pr",
                "list",
                "--repo",
                occ_repo,
                "--state",
                "open",
                "--label",
                MACHINE_MINTED_LABEL,
                "--limit",
                str(_OPEN_PR_LIMIT),
                "--json",
                "number",
            ]
        )
        if not isinstance(payload, list):
            raise RuntimeError(
                f"gh pr list returned {type(payload).__name__}, not a list of PRs"
            )
        numbers = tuple(
            int(e["number"])
            for e in payload
            if isinstance(e, dict) and isinstance(e.get("number"), int)
        )
        if len(numbers) >= _OPEN_PR_LIMIT:
            raise RuntimeError(
                f"gh pr list returned {len(numbers)} companions, at the "
                f"{_OPEN_PR_LIMIT} cap; this pass would be silently partial"
            )
        return numbers

    def companion(self, *, occ_repo: str, number: int) -> CompanionFacts:
        pr = _obj(self._json(["api", f"repos/{occ_repo}/pulls/{number}"]))
        # GitHub computes mergeability lazily: the first read of a PR whose base
        # moved usually answers null and starts the computation. Read again a
        # bounded number of times rather than report every companion unknown.
        for _ in range(_MERGEABILITY_READS):
            if isinstance(pr.get("mergeable"), bool):
                break
            self._sleep(_MERGEABILITY_WAIT_SECONDS)
            pr = _obj(self._json(["api", f"repos/{occ_repo}/pulls/{number}"]))
        head = _obj(pr.get("head"))
        head_sha = str(head.get("sha") or "")
        commit = _obj(self._json(["api", f"repos/{occ_repo}/commits/{head_sha}"]))
        committer = _obj(_obj(commit.get("commit")).get("committer"))
        # --paginate emits one filename per line across every page; parsing a
        # per-page JSON array instead would break on the second page.
        listing = self._run(
            [
                "api",
                "--paginate",
                f"repos/{occ_repo}/pulls/{number}/files?per_page=100",
                "--jq",
                ".[].filename",
            ]
        )
        if listing.returncode != 0:
            raise RuntimeError(
                f"listing OCC#{number} files exited {listing.returncode}: "
                f"{listing.stderr.strip()}"
            )
        names = tuple(ln.strip() for ln in listing.stdout.splitlines() if ln.strip())
        mergeable = pr.get("mergeable")
        labels = pr.get("labels")
        return CompanionFacts(
            number=number,
            title=str(pr.get("title") or ""),
            head_ref=str(head.get("ref") or ""),
            head_sha=head_sha,
            draft=bool(pr.get("draft")),
            labels=tuple(
                str(lbl.get("name"))
                for lbl in (labels if isinstance(labels, list) else [])
                if isinstance(lbl, dict)
            ),
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            mergeable_state=str(pr.get("mergeable_state") or ""),
            head_committed_at=_parse_ts(committer.get("date")),
            files=names,
        )

    def newest_commit_since(
        self, *, occ_repo: str, path: str, since: datetime
    ) -> datetime | None:
        stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = self._json(
            [
                "api",
                f"repos/{occ_repo}/commits?sha={OCC_BASE_BRANCH}&path={path}"
                f"&since={stamp}&per_page=100",
            ]
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"commits for {path}: payload is not a list")
        newest: datetime | None = None
        for entry in payload:
            committer = _obj(_obj(_obj(entry).get("commit")).get("committer"))
            if not committer:
                continue
            when = _parse_ts(committer.get("date"))
            # ``since`` filters on the author date; compare the committer date,
            # which is when the squash landed on the base branch.
            if when > since and (newest is None or when > newest):
                newest = when
        return newest

    def product(self, *, repo: str, pr_number: int) -> ProductFacts:
        completed = self._run(["api", f"repos/{repo}/pulls/{pr_number}"])
        if completed.returncode != 0:
            if "HTTP 404" in completed.stderr or "HTTP 403" in completed.stderr:
                return ProductFacts(readable=False)
            raise RuntimeError(
                f"reading {repo}#{pr_number} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        pr = _obj(json.loads(completed.stdout or "{}"))
        head = _obj(pr.get("head"))
        return ProductFacts(
            readable=True,
            state=str(pr.get("state") or ""),
            merged=bool(pr.get("merged")) or bool(pr.get("merged_at")),
            draft=bool(pr.get("draft")),
            body=str(pr.get("body") or ""),
            head_sha=str(head.get("sha") or ""),
            title=str(pr.get("title") or ""),
        )

    def receipt_runner_ran_since(
        self, *, repo: str, pr_number: int, since: datetime
    ) -> bool:
        stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = self._json(
            [
                "api",
                f"repos/{repo}/actions/workflows/{RECEIPT_RUNNER_WORKFLOW}/runs"
                f"?created=%3E%3D{stamp}&per_page=100",
            ]
        )
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise RuntimeError("receipt runner runs payload has no workflow_runs list")
        name = RECEIPT_RUNNER_RUN_NAME.format(pr=pr_number)
        return any(
            isinstance(run, dict) and run.get("display_title") == name for run in runs
        )

    def publish_autobind(
        self, *, target: Target, product: ProductFacts, lane: str
    ) -> str:
        # The per-PR branch is what conflicted; the default ticket batch
        # command would address a different companion. This is the one
        # caller that asks for the per-PR path, and it does so by flag.
        return self._publish(
            target=target,
            head_sha=product.head_sha,
            title=product.title,
            lane=lane,
            extra_args=["--batch-mode", "off"],
        )

    def publish_window_autobind(
        self, *, target: Target, member: MemberFacts, lane: str
    ) -> str:
        # No flag: the publisher's default is the repository batch window, the
        # same command the product PR's own ready flip publishes.
        return self._publish(
            target=target,
            head_sha=member.head_sha,
            title=member.title,
            lane=lane,
            extra_args=[],
        )

    def open_members(self, *, repo: str) -> tuple[MemberFacts, ...]:
        listing = self._run(
            [
                "api",
                "--paginate",
                f"repos/{repo}/pulls?state=open&per_page=100",
                "--jq",
                ".[] | {number, title, body, draft, sha: .head.sha} | @json",
            ]
        )
        if listing.returncode != 0:
            raise RuntimeError(
                f"listing open {repo} PRs exited {listing.returncode}: "
                f"{listing.stderr.strip()}"
            )
        members: list[MemberFacts] = []
        for line in listing.stdout.splitlines():
            if not line.strip():
                continue
            row = _obj(json.loads(line))
            number = row.get("number")
            if not isinstance(number, int):
                raise RuntimeError(f"open {repo} PR row has no number: {line[:200]}")
            members.append(
                MemberFacts(
                    number=number,
                    title=str(row.get("title") or ""),
                    body=str(row.get("body") or ""),
                    draft=bool(row.get("draft")),
                    head_sha=str(row.get("sha") or ""),
                )
            )
        return tuple(members)

    def head_committed_at(self, *, repo: str, sha: str) -> datetime:
        commit = _obj(self._json(["api", f"repos/{repo}/commits/{sha}"]))
        committer = _obj(_obj(commit.get("commit")).get("committer"))
        return _parse_ts(committer.get("date"))

    def open_window_number(self, *, occ_repo: str, branch: str) -> int | None:
        payload = self._json(
            [
                "pr",
                "list",
                "--repo",
                occ_repo,
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "number",
            ]
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"gh pr list for {branch} returned no list")
        numbers = sorted(
            int(e["number"])
            for e in payload
            if isinstance(e, dict) and isinstance(e.get("number"), int)
        )
        return numbers[0] if numbers else None

    def _publish(
        self,
        *,
        target: Target,
        head_sha: str,
        title: str,
        lane: str,
        extra_args: list[str],
    ) -> str:
        env = dict(os.environ)
        env.update(
            {
                "PR_REPO": target.repo,
                "PR_NUMBER": str(target.pr_number),
                "PR_HEAD_SHA": head_sha or UNREAD_HEAD_SHA,
                "PR_TITLE": title,
                "PR_TICKET": target.ticket,
                "RUNNER_IS_TRUSTED": "true",
            }
        )
        # The retired batch switch is refused by the publisher (OMN-16336); an
        # inherited value must not stop a re-mint.
        env.pop("OCC_COMPANION_BATCH_MODE", None)
        completed = subprocess.run(
            [sys.executable, str(self._publisher), "--lane", lane, *extra_args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0 or "Published " not in completed.stdout:
            raise RuntimeError(
                f"autobind publish for {target.repo}#{target.pr_number} did not "
                f"deliver (exit {completed.returncode}): {output.strip()[-800:]}"
            )
        published = [
            ln for ln in completed.stdout.splitlines() if ln.startswith("Published ")
        ]
        return published[-1]

    def dispatch_receipt_runner(self, *, repo: str, pr_number: int) -> None:
        completed = self._run(
            [
                "workflow",
                "run",
                RECEIPT_RUNNER_WORKFLOW,
                "--repo",
                repo,
                "--ref",
                "dev",
                "-f",
                f"pr_number={pr_number}",
            ]
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"dispatching {RECEIPT_RUNNER_WORKFLOW} for {repo}#{pr_number} "
                f"exited {completed.returncode}: {completed.stderr.strip()}"
            )


@dataclass(frozen=True)
class PassReport:
    remint: tuple[tuple[int, RemintDecision], ...]
    receipts: tuple[tuple[int, ReceiptDecision], ...]
    errors: tuple[str, ...]


def run_pass(
    gh: GhPort,
    *,
    occ_repo: str,
    this_repo: str,
    lane: str,
    dry_run: bool,
    only: int | None,
    now: datetime,
) -> PassReport:
    """One sweep over the open machine-minted companions. Returns every verdict."""
    remint: list[tuple[int, RemintDecision]] = []
    receipts: list[tuple[int, ReceiptDecision]] = []
    errors: list[str] = []
    numbers = gh.open_companion_numbers(occ_repo=occ_repo)
    if only is not None:
        numbers = tuple(n for n in numbers if n == only)
    for number in numbers:
        try:
            companion = gh.companion(occ_repo=occ_repo, number=number)
            resolved = resolve_target(companion)
            target = resolved if isinstance(resolved, Target) else None
            product = (
                gh.product(repo=target.repo, pr_number=target.pr_number)
                if target is not None
                else ProductFacts(readable=False)
            )
            newest: datetime | None = None
            if companion.mergeable is False and companion.mergeable_state == "dirty":
                for path in companion.contract_paths:
                    when = gh.newest_commit_since(
                        occ_repo=occ_repo, path=path, since=companion.head_committed_at
                    )
                    if when is not None and (newest is None or when > newest):
                        newest = when
            decision = decide_remint(companion, newest, product)
            if decision.outcome is EnumRemintOutcome.REMINT and not dry_run:
                assert decision.target is not None
                delivered = gh.publish_autobind(
                    target=decision.target, product=product, lane=lane
                )
                decision = RemintDecision(
                    decision.outcome, f"{decision.reason}; {delivered}", decision.target
                )
            remint.append((number, decision))

            ran = False
            if (
                target is not None
                and target.repo.lower() == this_repo.lower()
                and companion.mergeable is True
                and pending_receipt_dirs(companion.files)
            ):
                ran = gh.receipt_runner_ran_since(
                    repo=this_repo,
                    pr_number=target.pr_number,
                    since=companion.head_committed_at,
                )
            receipt = decide_receipt_refire(
                companion,
                target,
                product,
                this_repo=this_repo,
                runner_ran_since_head=ran,
                now=now,
            )
            if receipt.outcome is EnumReceiptOutcome.DISPATCH and not dry_run:
                assert target is not None
                gh.dispatch_receipt_runner(repo=this_repo, pr_number=target.pr_number)
            receipts.append((number, receipt))
        except Exception as exc:  # report every companion, then fail the job
            errors.append(f"OCC#{number}: {exc}")
    return PassReport(tuple(remint), tuple(receipts), tuple(errors))


@dataclass(frozen=True)
class RedriveReport:
    decisions: tuple[tuple[int, RedriveDecision], ...]
    open_window: int | None
    errors: tuple[str, ...]


def run_window_redrive_pass(
    gh: RedrivePort,
    *,
    occ_repo: str,
    this_repo: str,
    lane: str,
    dry_run: bool,
    now: datetime,
) -> RedriveReport:
    """Republish the window autobind command for this repo's stranded members.

    Every eligible member is published, oldest PR first, in one pass. The first
    the emitter accepts opens the window; the rest meet that window in flight
    and are skipped by the emitter's own hold, to be re-driven after it merges.
    Publishing all of them, not only the oldest, keeps a member the emitter
    declines for good (a hold marker, no derivable check) from starving the
    members behind it.
    """
    decisions: list[tuple[int, RedriveDecision]] = []
    errors: list[str] = []
    open_window = gh.open_window_number(
        occ_repo=occ_repo, branch=window_companion_branch_for(this_repo)
    )
    for member in sorted(gh.open_members(repo=this_repo), key=lambda m: m.number):
        try:
            if (
                not member.draft
                and not _EVIDENCE_SOURCE_RE.search(member.body)
                and member.head_sha
            ):
                member = replace(
                    member,
                    head_committed_at=gh.head_committed_at(
                        repo=this_repo, sha=member.head_sha
                    ),
                )
            decision = decide_window_redrive(
                member, repo=this_repo, open_window=open_window, now=now
            )
            if decision.outcome is EnumRedriveOutcome.REDRIVE and not dry_run:
                assert decision.target is not None
                delivered = gh.publish_window_autobind(
                    target=decision.target, member=member, lane=lane
                )
                decision = replace(decision, reason=f"{decision.reason}; {delivered}")
            decisions.append((member.number, decision))
        except Exception as exc:  # report every member, then fail the job
            errors.append(f"{this_repo}#{member.number}: {exc}")
    return RedriveReport(tuple(decisions), open_window, tuple(errors))


def _render_redrive(report: RedriveReport, *, dry_run: bool) -> str:
    verb = "would act" if dry_run else "acted"
    window = f"OCC#{report.open_window}" if report.open_window else "none"
    lines = [
        f"window re-drive: considered {len(report.decisions)} open PR(s), open "
        f"window {window} ({verb} on marked rows)"
    ]
    for number, decision in report.decisions:
        if decision.outcome in (EnumRedriveOutcome.DRAFT, EnumRedriveOutcome.BOUND):
            continue
        mark = "*" if decision.outcome is EnumRedriveOutcome.REDRIVE else " "
        lines.append(
            f"{mark} #{number} redrive={decision.outcome.value}: {decision.reason}"
        )
    for error in report.errors:
        lines.append(f"ERROR {error}")
    return "\n".join(lines)


def _render(report: PassReport, *, dry_run: bool) -> str:
    verb = "would act" if dry_run else "acted"
    lines = [
        f"considered {len(report.remint)} machine-minted companion(s) ({verb} on marked rows)"
    ]
    for number, decision in report.remint:
        mark = "*" if decision.outcome is EnumRemintOutcome.REMINT else " "
        lines.append(
            f"{mark} OCC#{number} remint={decision.outcome.value}: {decision.reason}"
        )
    for number, receipt in report.receipts:
        if receipt.outcome in (
            EnumReceiptOutcome.OTHER_REPO,
            EnumReceiptOutcome.NO_PENDING_RECEIPT,
        ):
            continue
        mark = "*" if receipt.outcome is EnumReceiptOutcome.DISPATCH else " "
        lines.append(
            f"{mark} OCC#{number} receipts={receipt.outcome.value}: {receipt.reason}"
        )
    for error in report.errors:
        lines.append(f"ERROR {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", required=True, help="this repository (owner/name)")
    parser.add_argument("--occ-repo", default=OCC_REPO_DEFAULT)
    parser.add_argument("--lane", default="dev")
    parser.add_argument("--occ-pr", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publisher",
        default=str(
            Path(__file__).resolve().parents[1] / "publish_occ_autobind_command.py"
        ),
    )
    args = parser.parse_args(argv)
    gh = GhCli(Path(args.publisher))
    report = run_pass(
        gh,
        occ_repo=args.occ_repo,
        this_repo=args.repo,
        lane=args.lane,
        dry_run=args.dry_run,
        only=args.occ_pr,
        now=datetime.now(UTC),
    )
    text = _render(report, dry_run=args.dry_run)
    errors = report.errors
    # A run scoped to one companion is a targeted re-mint; the window re-drive
    # (OMN-17427) belongs to the full scheduled pass only.
    if args.occ_pr is None:
        redrive = run_window_redrive_pass(
            gh,
            occ_repo=args.occ_repo,
            this_repo=args.repo,
            lane=args.lane,
            dry_run=args.dry_run,
            now=datetime.now(UTC),
        )
        text = f"{text}\n{_render_redrive(redrive, dry_run=args.dry_run)}"
        errors = (*errors, *redrive.errors)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary:
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write("```\n" + text + "\n```\n")
    return EXIT_ERROR if errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
