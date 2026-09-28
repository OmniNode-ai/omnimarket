# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Clear two omnimarket reds whose cause has already gone away (OMN-19888).

Why this module exists
----------------------
Two required reds on omnimarket pull requests are caused by something OUTSIDE
the pull request, and neither one clears when that cause goes away, because the
fact that clears it lands somewhere nothing on the PR listens to. Measured live
on 2026-09-27 by the stall diagnosis under OMN-19852 (ledger TERMINAL
2026-09-27T16:07:43Z lane=market-armed-stall-83, findings 3 and 4).

**The release window.** Measured 2026-09-27, when every dev merge published a
release (``release-on-merge.yml``): the post-release version bump PR that moves
dev off the published version lands ten to fourteen minutes later, and between
the two, dev's ``project.version`` equals the newest published tag, so any PR
whose CI starts in that window fails ``Release Identity Gate`` with ``pyproject
version X is NOT ahead of the latest published version X``. From 11:30Z to
14:27Z on 2026-09-27 dev sat inside a window about 49 percent of the time
(omnimarket#3009, #2956, #2955 and #2905 were each synced inside one). A rerun
cannot clear it: ``pull_request`` runs replay the merge ref GitHub pinned at
trigger time, which still carries the published version. Only a new merge ref
does, which means a branch update.

``release-on-merge.yml`` is retired as of OMN-18010: releases are now cut on an
explicit ``workflow_dispatch`` (``release-cut.yml``), so this window opens at
most once per manual cut rather than once per merge. The mechanics — and this
heal — are unchanged; only its trigger frequency is expected to fall sharply.

**Vendor parity.** ``node-migration-vendor-parity-gate`` fails an omnimarket PR
that adds a node migration until the omnibase_infra PR that vendors the same
bytes merges to infra ``dev``. The gate reads infra ``dev`` live at run time,
so a rerun after the vendor merge passes, but nothing issues one: six market
PRs sat red on 2026-09-27 behind infra vendor PRs, and when omnibase_infra#4206
merged at 16:01Z, omnimarket#3014 stayed red until a lane re-ran it by hand.
``CI Summary`` asserts the gate as an external context (``ci_summary_gate.py``
``EXPECTED_EXTERNAL_CONTEXTS``), so its own job has to be re-run as well.

What this does
--------------
Two pure decision functions, one per heal, over inputs a :class:`GhPort` reads:

* :func:`decide_release_window` returns ``update_required`` for an open,
  non-draft, non-conflicted PR based on ``dev`` whose newest ``Release Identity
  Gate`` check-run on its current head FAILED naming a version below the one
  dev now carries, while dev is OUT of the window (dev's version strictly ahead
  of the newest published tag). The branch update is pinned to the head the
  decision read (``expected_head_sha``), so it is issued at most once per head:
  a moved head refuses it, and the new head gets its own CI.
* :func:`decide_vendor_parity` returns ``rerun_required`` for a PR whose newest
  ``node-migration-vendor-parity-gate`` check-run FAILED, when every migration
  that run named is now byte-identical on omnibase_infra ``dev`` AND the newest
  infra commit touching each vendored copy landed AFTER the gate failed. A gate
  that failed after the vendor landed read the vendored bytes and failed anyway:
  that is a real red, and it is left alone. That precondition is also what makes
  the heal once per head: the re-run's own failure, if any, postdates the vendor.

Both refusals are enumerated one outcome per branch, as in
``occ_companion_merge_heal.py`` (OMN-18812): a heal that says "nothing to do"
for both "nothing was stale" and "I could not tell" is the defect that module
was rebuilt to remove, so an unreadable state is its own outcome and a pass
that cannot read the open-PR list exits non-zero.

Why not ``bulk_pr_throttle.py``
-------------------------------
Bulk update-branch and rerun from a LANE go through omnibase_infra's
``scripts/ci/bulk_pr_throttle.py``. This is a scheduled workflow, not a lane,
and the throttle cannot serve it: its runner-capacity probe needs an org-read
credential this repository's workflow token does not hold, the one credential
that does (``CROSS_REPO_PAT``) is being retired under OMN-16373, and its
update-branch cannot pin ``expected_head_sha``. The bound the throttle exists
to impose is imposed here instead, per pass: at most
:data:`MAX_UPDATES_PER_PASS` branch updates and :data:`MAX_RERUN_PRS_PER_PASS`
re-run PRs, the rest deferred to the next pass ten minutes later, and every
action gated on a precondition that makes a repeat on the same head impossible.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess  # fixed argv, no shell, trusted gh binary
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1

INFRA_REPO_DEFAULT: Final[str] = "OmniNode-ai/omnibase_infra"
DEV_BRANCH: Final[str] = "dev"

RELEASE_IDENTITY_CHECK: Final[str] = "Release Identity Gate"
VENDOR_PARITY_CHECK: Final[str] = "node-migration-vendor-parity-gate"
CI_SUMMARY_CHECK: Final[str] = "CI Summary"

#: Per-pass bounds (see "Why not bulk_pr_throttle.py" above). A branch update
#: starts a whole fresh CI suite, so it is the tighter of the two.
MAX_UPDATES_PER_PASS: Final[int] = 5
MAX_RERUN_PRS_PER_PASS: Final[int] = 10

#: Backstop against an unforeseen loop, mirroring OMN-18812's ceiling. The
#: vendored-after-failure precondition is expected to do the actual work.
MAX_HEAL_RUN_ATTEMPT: Final[int] = 5

_PAGE_SIZE: Final[int] = 100
_OPEN_PR_LIMIT: Final[int] = 2000

#: ``scripts/check_release_identity.py``'s refusal line. Both versions are
#: captured: the first is the merge ref's ``project.version``, the second the
#: newest published tag reachable from it.
RELEASE_IDENTITY_FAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"FAIL: packaged source changed but pyproject version (\d+(?:\.\d+)*) is NOT "
    r"ahead of the latest published version (\d+(?:\.\d+)*)"
)

#: The vendor parity gate's two per-file refusals, as annotations. Both begin
#: with the omnimarket source path; the destination is derived from it exactly
#: as the gate derives it.
VENDOR_PARITY_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(src/omnimarket/nodes/([^/\s]+)/migrations/([^/\s]+\.sql)) "
    r"(?:has no vendored counterpart|differs from the vendored copy)"
)

_RUN_ID_IN_DETAILS_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"/actions/runs/(\d+)/job/(\d+)"
)
_TAG_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_RED_JOB_CONCLUSIONS: Final[frozenset[str]] = frozenset({"failure", "timed_out"})


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class EnumReleaseWindowOutcome(StrEnum):
    """Why the release-window heal did or did not update a branch."""

    UPDATE_REQUIRED = "update_required"
    GATE_NOT_RED = "gate_not_red"
    DRAFT = "draft"
    NOT_DEV_BASE = "not_dev_base"
    CONFLICTED = "conflicted"
    FAILURE_UNPARSED = "failure_unparsed"
    DEV_STATE_UNRESOLVED = "dev_state_unresolved"
    DEV_IN_WINDOW = "dev_in_window"
    VERSION_NOT_BELOW_DEV = "version_not_below_dev"


class EnumVendorParityOutcome(StrEnum):
    """Why the vendor-parity heal did or did not re-run a gate."""

    RERUN_REQUIRED = "rerun_required"
    GATE_NOT_RED = "gate_not_red"
    DRAFT = "draft"
    SUPERSEDED_BY_UPDATE = "superseded_by_update"
    NO_MIGRATION_NAMED = "no_migration_named"
    VENDOR_STATE_UNRESOLVED = "vendor_state_unresolved"
    NOT_VENDORED = "not_vendored"
    VENDORED_COPY_DIFFERS = "vendored_copy_differs"
    FAILED_AFTER_VENDOR = "failed_after_vendor"
    ATTEMPT_CEILING = "attempt_ceiling"


class EnumVendorState(StrEnum):
    """One migration's state on infra ``dev`` against the PR head's bytes."""

    IDENTICAL = "identical"
    MISSING = "missing"
    DIFFERS = "differs"
    UNRESOLVED = "unresolved"


# ---------------------------------------------------------------------------
# Inputs and decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenPr:
    """The listing fields both heals read."""

    number: int
    head_sha: str
    is_draft: bool = False
    base_ref: str = DEV_BRANCH
    #: GraphQL ``mergeStateStatus``. ``DIRTY`` is a real conflict; anything
    #: else (including ``UNKNOWN``, which GitHub computes lazily) proceeds and
    #: lets the update-branch call itself refuse a conflict.
    merge_state: str = "UNKNOWN"


@dataclass(frozen=True)
class GateSnapshot:
    """The newest check-run of one name on a head."""

    check_run_id: int
    status: str
    conclusion: str
    completed_at: str = ""
    run_id: int | None = None

    @property
    def red(self) -> bool:
        return self.status == "completed" and self.conclusion == "failure"


@dataclass(frozen=True)
class DevReleaseState:
    """Dev's ``project.version`` and the newest published tag."""

    dev_version: tuple[int, ...]
    latest_published: tuple[int, ...]

    @property
    def in_window(self) -> bool:
        """Dev is level with (or behind) the newest release: armed against itself."""
        return self.dev_version <= self.latest_published


@dataclass(frozen=True)
class ReleaseWindowInput:
    pr: OpenPr
    gate: GateSnapshot | None
    #: ``(merge-ref version, published version)`` from the failure log, or
    #: ``None`` when the log carried no refusal line this module recognises.
    failed_versions: tuple[tuple[int, ...], tuple[int, ...]] | None
    dev: DevReleaseState | None


@dataclass(frozen=True)
class MigrationVendorState:
    src_path: str
    dest_path: str
    state: EnumVendorState
    #: ISO-8601 time the newest infra ``dev`` commit touching ``dest_path``
    #: reached ``dev`` (its PR's merge time); ``""`` when unread.
    vendored_at: str = ""


@dataclass(frozen=True)
class CiSummarySnapshot:
    """``CI Summary``'s newest check-run and whether its run has other reds."""

    job_id: int
    run_id: int | None
    red: bool
    other_red_jobs: int = 0


@dataclass(frozen=True)
class VendorParityInput:
    pr: OpenPr
    gate: GateSnapshot | None
    run_attempt: int
    migrations: tuple[MigrationVendorState, ...]
    ci_summary: CiSummarySnapshot | None = None
    superseded_by_update: bool = False


@dataclass(frozen=True)
class ReleaseWindowDecision:
    outcome: EnumReleaseWindowOutcome
    pr_number: int
    detail: str
    head_sha: str = ""

    @property
    def act(self) -> bool:
        return self.outcome is EnumReleaseWindowOutcome.UPDATE_REQUIRED


@dataclass(frozen=True)
class VendorParityDecision:
    outcome: EnumVendorParityOutcome
    pr_number: int
    detail: str
    #: Runs re-run whole. The gate's workflow has exactly one job.
    run_ids: tuple[int, ...] = field(default_factory=tuple)
    #: Jobs re-run alone: ``CI Summary``, which asserts the gate externally.
    job_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def act(self) -> bool:
        return self.outcome is EnumVendorParityOutcome.RERUN_REQUIRED


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def parse_version(raw: str) -> tuple[int, ...] | None:
    """A dotted release version as an int tuple, or ``None``.

    Deliberately narrow: this repo publishes plain ``X.Y.Z``. Anything else
    (a pre-release suffix, a local segment) is refused rather than guessed,
    which leaves the PR alone.
    """
    match = _TAG_VERSION_RE.match((raw or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def parse_release_identity_failure(
    log_text: str,
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """``(merge-ref version, published version)`` from a gate log, or ``None``."""
    match = RELEASE_IDENTITY_FAIL_RE.search(log_text or "")
    if match is None:
        return None
    ours = parse_version(match.group(1))
    published = parse_version(match.group(2))
    if ours is None or published is None:
        return None
    return ours, published


def latest_published_version(tag_names: list[str]) -> tuple[int, ...] | None:
    """The highest ``vX.Y.Z`` (or bare ``X.Y.Z``) tag, or ``None``."""
    versions = [v for v in (parse_version(name) for name in tag_names) if v]
    return max(versions) if versions else None


def pyproject_version(text: str) -> tuple[int, ...] | None:
    """``[project].version`` of a ``pyproject.toml`` body, or ``None``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    raw = project.get("version")
    return parse_version(raw) if isinstance(raw, str) else None


def vendor_migrations_named(annotations: list[dict[str, object]]) -> list[str]:
    """The omnimarket migration paths a failed parity run named, in order."""
    out: list[str] = []
    for entry in annotations:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, str):
            continue
        match = VENDOR_PARITY_FILE_RE.match(message.strip())
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def vendored_dest_path(src_path: str) -> str:
    """The omnibase_infra path the parity gate compares ``src_path`` against."""
    match = re.match(
        r"^src/omnimarket/nodes/([^/]+)/migrations/([^/]+\.sql)$", src_path
    )
    if match is None:
        raise ValueError(f"not a node migration path: {src_path!r}")
    return f"docker/migrations/forward/nodes/{match.group(1)}/{match.group(2)}"


def newest_check_run(
    check_runs: list[dict[str, object]], name: str
) -> GateSnapshot | None:
    """The newest check-run named ``name``, by id (ids are monotonic)."""
    best: dict[str, object] | None = None
    best_id = -1
    for run in check_runs:
        if not isinstance(run, dict) or run.get("name") != name:
            continue
        run_id = run.get("id")
        if isinstance(run_id, int) and run_id > best_id:
            best, best_id = run, run_id
    if best is None:
        return None
    details = str(best.get("details_url") or "")
    match = _RUN_ID_IN_DETAILS_URL_RE.search(details)
    return GateSnapshot(
        check_run_id=best_id,
        status=str(best.get("status") or ""),
        conclusion=str(best.get("conclusion") or ""),
        completed_at=str(best.get("completed_at") or ""),
        run_id=int(match.group(1)) if match else None,
    )


def _parse_timestamp(value: str) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def landed_after(vendored_at: str, failed_at: str) -> bool:
    """Whether the vendor commit landed strictly after the gate failed.

    Either timestamp unreadable resolves to ``False``: the PR is left alone. A
    missed heal costs the hand re-run that happens today; a spurious one
    re-runs a verdict that already read the vendored bytes.
    """
    vendored = _parse_timestamp(vendored_at)
    failed = _parse_timestamp(failed_at)
    if vendored is None or failed is None:
        return False
    return vendored > failed


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------


def decide_release_window(item: ReleaseWindowInput) -> ReleaseWindowDecision:
    """Decide whether one PR's branch must be updated to leave a release window.

    Branch order is cheapest refusal first. The dev state is consulted last,
    and an unreadable dev state refuses rather than guesses.
    """
    pr = item.pr
    where = f"PR #{pr.number} (head {pr.head_sha[:12]})"

    def refuse(outcome: EnumReleaseWindowOutcome, why: str) -> ReleaseWindowDecision:
        return ReleaseWindowDecision(
            outcome=outcome, pr_number=pr.number, detail=f"{where}: {why}"
        )

    if item.gate is None or not item.gate.red:
        state = "absent" if item.gate is None else item.gate.conclusion or "pending"
        return refuse(
            EnumReleaseWindowOutcome.GATE_NOT_RED,
            f"{RELEASE_IDENTITY_CHECK} is {state} on this head",
        )
    if pr.is_draft:
        return refuse(
            EnumReleaseWindowOutcome.DRAFT,
            "draft; a branch update would spend a full CI suite on a PR not landing",
        )
    if pr.base_ref != DEV_BRANCH:
        return refuse(
            EnumReleaseWindowOutcome.NOT_DEV_BASE,
            f"based on {pr.base_ref!r}, not {DEV_BRANCH!r}; its version comes "
            "from that base, which this heal does not read",
        )
    if pr.merge_state.upper() == "DIRTY":
        return refuse(
            EnumReleaseWindowOutcome.CONFLICTED,
            "conflicts with its base; update-branch cannot merge it, and a "
            "person has to resolve the conflict",
        )
    if item.failed_versions is None:
        return refuse(
            EnumReleaseWindowOutcome.FAILURE_UNPARSED,
            f"{RELEASE_IDENTITY_CHECK} failed but its log carries no "
            "release-window refusal line, so the red is not this heal's class",
        )
    if item.dev is None:
        return refuse(
            EnumReleaseWindowOutcome.DEV_STATE_UNRESOLVED,
            "dev's version or the newest published tag could not be read",
        )
    ours, published = item.failed_versions
    if item.dev.in_window:
        return refuse(
            EnumReleaseWindowOutcome.DEV_IN_WINDOW,
            f"dev is at {_fmt(item.dev.dev_version)} with "
            f"v{_fmt(item.dev.latest_published)} published, still inside a "
            "release window; an update now would fail the same way",
        )
    if ours >= item.dev.dev_version:
        return refuse(
            EnumReleaseWindowOutcome.VERSION_NOT_BELOW_DEV,
            f"the failure named {_fmt(ours)}, not below dev's "
            f"{_fmt(item.dev.dev_version)}; an update would not change the verdict",
        )
    return ReleaseWindowDecision(
        outcome=EnumReleaseWindowOutcome.UPDATE_REQUIRED,
        pr_number=pr.number,
        head_sha=pr.head_sha,
        detail=(
            f"{where}: {RELEASE_IDENTITY_CHECK} failed at {_fmt(ours)} (published "
            f"{_fmt(published)}); dev is now {_fmt(item.dev.dev_version)} ahead of "
            f"v{_fmt(item.dev.latest_published)}; updating the branch at this head"
        ),
    )


def decide_vendor_parity(item: VendorParityInput) -> VendorParityDecision:
    """Decide whether one PR's parity gate (and CI Summary) must be re-run."""
    pr = item.pr
    where = f"PR #{pr.number} (head {pr.head_sha[:12]})"

    def refuse(outcome: EnumVendorParityOutcome, why: str) -> VendorParityDecision:
        return VendorParityDecision(
            outcome=outcome, pr_number=pr.number, detail=f"{where}: {why}"
        )

    gate = item.gate
    if gate is None or not gate.red:
        state = "absent" if gate is None else gate.conclusion or "pending"
        return refuse(
            EnumVendorParityOutcome.GATE_NOT_RED,
            f"{VENDOR_PARITY_CHECK} is {state} on this head",
        )
    if pr.is_draft:
        return refuse(EnumVendorParityOutcome.DRAFT, "draft; not re-running")
    if item.superseded_by_update:
        return refuse(
            EnumVendorParityOutcome.SUPERSEDED_BY_UPDATE,
            "this pass updates the branch, and the new head's gate reads infra "
            "dev fresh",
        )
    if not item.migrations:
        return refuse(
            EnumVendorParityOutcome.NO_MIGRATION_NAMED,
            f"{VENDOR_PARITY_CHECK} failed without naming a migration this heal "
            "can check; the red is not this heal's class",
        )
    for migration in item.migrations:
        if migration.state is EnumVendorState.UNRESOLVED:
            return refuse(
                EnumVendorParityOutcome.VENDOR_STATE_UNRESOLVED,
                f"could not read {migration.src_path} or its vendored copy",
            )
    missing = [
        m.src_path for m in item.migrations if m.state is EnumVendorState.MISSING
    ]
    if missing:
        return refuse(
            EnumVendorParityOutcome.NOT_VENDORED,
            f"infra dev does not vendor {', '.join(missing)} yet",
        )
    differs = [
        m.src_path for m in item.migrations if m.state is EnumVendorState.DIFFERS
    ]
    if differs:
        return refuse(
            EnumVendorParityOutcome.VENDORED_COPY_DIFFERS,
            f"infra dev's copy of {', '.join(differs)} differs from this head's "
            "bytes; a re-run would fail the same way",
        )
    stale_vendor = [
        m.src_path
        for m in item.migrations
        if not landed_after(m.vendored_at, gate.completed_at)
    ]
    if stale_vendor:
        return refuse(
            EnumVendorParityOutcome.FAILED_AFTER_VENDOR,
            f"the gate failed at {gate.completed_at or '(unknown)'}, not before "
            f"the vendor commit for {', '.join(stale_vendor)} landed; it read the "
            "vendored bytes and failed anyway, which is a real red",
        )
    if gate.run_id is None:
        return refuse(
            EnumVendorParityOutcome.VENDOR_STATE_UNRESOLVED,
            "the gate's workflow run id could not be read from its check-run",
        )
    if item.run_attempt >= MAX_HEAL_RUN_ATTEMPT:
        return refuse(
            EnumVendorParityOutcome.ATTEMPT_CEILING,
            f"run {gate.run_id} is already at the {MAX_HEAL_RUN_ATTEMPT}-attempt "
            "ceiling; refusing to re-run",
        )

    job_ids: tuple[int, ...] = ()
    summary_note = "CI Summary is not red"
    summary = item.ci_summary
    if summary is not None and summary.red:
        if summary.other_red_jobs:
            summary_note = (
                f"CI Summary left alone: its run has {summary.other_red_jobs} "
                "other red job(s)"
            )
        else:
            job_ids = (summary.job_id,)
            summary_note = f"re-running CI Summary job {summary.job_id}"
    return VendorParityDecision(
        outcome=EnumVendorParityOutcome.RERUN_REQUIRED,
        pr_number=pr.number,
        run_ids=(gate.run_id,),
        job_ids=job_ids,
        detail=(
            f"{where}: every named migration is byte-identical on infra dev and "
            f"was vendored after the gate failed; re-running run {gate.run_id}; "
            f"{summary_note}"
        ),
    )


# ---------------------------------------------------------------------------
# GitHub port
# ---------------------------------------------------------------------------


class GhPort(Protocol):
    """The GitHub reads and writes both heals need."""

    def open_pull_requests(self, *, repo: str) -> tuple[OpenPr, ...]: ...

    def check_runs(self, *, repo: str, head_sha: str) -> list[dict[str, object]]: ...

    def job_log(self, *, repo: str, job_id: int) -> str: ...

    def job(self, *, repo: str, job_id: int) -> dict[str, object]: ...

    def run_jobs(self, *, repo: str, run_id: int) -> list[dict[str, object]]: ...

    def annotations(
        self, *, repo: str, check_run_id: int
    ) -> list[dict[str, object]]: ...

    def tag_names(self, *, repo: str) -> list[str]: ...

    def file_bytes(self, *, repo: str, path: str, ref: str) -> bytes | None:
        """The file's bytes at ``ref``; ``None`` when it does not exist there.

        Any other failure raises :class:`RuntimeError`.
        """
        ...

    def landed_at(self, *, repo: str, path: str, ref: str) -> str:
        """When the newest commit touching ``path`` reached ``ref`` (ISO-8601).

        The merge time of the PR that landed it when there is one, else the
        commit's committer date; ``""`` when unread.
        """
        ...

    def update_branch(
        self, *, repo: str, pr_number: int, expected_head_sha: str
    ) -> None: ...

    def rerun_run(self, *, repo: str, run_id: int) -> None: ...

    def rerun_job(self, *, repo: str, job_id: int) -> None: ...


def _pages(payload: object) -> list[object]:
    return payload if isinstance(payload, list) else [payload]


class GhCli:
    """:class:`GhPort` over the ``gh`` binary. Fixed argv, never a shell.

    ``update_token`` is the credential for the one write that must start CI.
    A branch update authored by the workflow's own ``GITHUB_TOKEN`` makes a
    head commit GitHub starts no workflow for (its anti-recursion rule), so the
    update would clear nothing and leave the PR with no checks at all. Reads
    and re-runs use the ambient ``GH_TOKEN``; a re-run keeps its run id and is
    not subject to that rule.
    """

    def __init__(self, *, update_token: str | None = None) -> None:
        self._update_token = update_token

    def _run(
        self, args: list[str], *, token: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = None
        if token is not None:
            env = {**os.environ, "GH_TOKEN": token}
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, env=env
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

    def _paged_list(self, path: str, key: str | None) -> list[dict[str, object]]:
        payload = self._json(["api", path, "--paginate", "--slurp"])
        out: list[dict[str, object]] = []
        for page in _pages(payload):
            items = page.get(key) if key and isinstance(page, dict) else page
            if isinstance(items, list):
                out.extend(entry for entry in items if isinstance(entry, dict))
        return out

    def open_pull_requests(self, *, repo: str) -> tuple[OpenPr, ...]:
        payload = self._json(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(_OPEN_PR_LIMIT),
                "--json",
                "number,headRefOid,isDraft,baseRefName,mergeStateStatus",
            ]
        )
        if not isinstance(payload, list):
            raise RuntimeError(
                f"gh pr list returned {type(payload).__name__}, not a list of pull requests"
            )
        out: list[OpenPr] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            number, head = entry.get("number"), entry.get("headRefOid")
            if not isinstance(number, int) or not isinstance(head, str):
                continue
            out.append(
                OpenPr(
                    number=number,
                    head_sha=head,
                    is_draft=bool(entry.get("isDraft") or False),
                    base_ref=str(entry.get("baseRefName") or ""),
                    merge_state=str(entry.get("mergeStateStatus") or "UNKNOWN"),
                )
            )
        if len(out) >= _OPEN_PR_LIMIT:
            raise RuntimeError(
                f"gh pr list returned {len(out)} open PRs, at the {_OPEN_PR_LIMIT} "
                "cap; this pass would be silently partial"
            )
        return tuple(out)

    def check_runs(self, *, repo: str, head_sha: str) -> list[dict[str, object]]:
        return self._paged_list(
            f"repos/{repo}/commits/{head_sha}/check-runs?per_page={_PAGE_SIZE}",
            "check_runs",
        )

    def job_log(self, *, repo: str, job_id: int) -> str:
        path = f"repos/{repo}/actions/jobs/{job_id}/logs"
        # gh >= 2.100 refuses to print a body carrying terminal escapes unless
        # told to; an older gh does not know the flag. Try the flag, fall back.
        completed = self._run(["api", "--allow-escape-sequences", path])
        if completed.returncode != 0 and "unknown flag" in completed.stderr:
            completed = self._run(["api", path])
        if completed.returncode != 0:
            raise RuntimeError(
                f"gh api {path} exited {completed.returncode}: {completed.stderr.strip()}"
            )
        return completed.stdout

    def job(self, *, repo: str, job_id: int) -> dict[str, object]:
        payload = self._json(["api", f"repos/{repo}/actions/jobs/{job_id}"])
        if not isinstance(payload, dict):
            raise RuntimeError(f"job {job_id} payload is not an object")
        return payload

    def run_jobs(self, *, repo: str, run_id: int) -> list[dict[str, object]]:
        return self._paged_list(
            f"repos/{repo}/actions/runs/{run_id}/jobs?per_page={_PAGE_SIZE}", "jobs"
        )

    def annotations(self, *, repo: str, check_run_id: int) -> list[dict[str, object]]:
        return self._paged_list(
            f"repos/{repo}/check-runs/{check_run_id}/annotations?per_page={_PAGE_SIZE}",
            None,
        )

    def tag_names(self, *, repo: str) -> list[str]:
        refs = self._paged_list(f"repos/{repo}/git/matching-refs/tags/v", None)
        return [
            str(ref.get("ref") or "").removeprefix("refs/tags/")
            for ref in refs
            if isinstance(ref.get("ref"), str)
        ]

    def file_bytes(self, *, repo: str, path: str, ref: str) -> bytes | None:
        completed = self._run(["api", f"repos/{repo}/contents/{path}?ref={ref}"])
        if completed.returncode != 0:
            if "HTTP 404" in completed.stderr or '"status":"404"' in completed.stdout:
                return None
            raise RuntimeError(
                f"reading {repo}:{path}@{ref} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        try:
            payload = json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"reading {repo}:{path}@{ref}: non-JSON: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise RuntimeError(f"reading {repo}:{path}@{ref}: not a base64 file body")
        try:
            return base64.b64decode(str(payload.get("content") or ""))
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(
                f"reading {repo}:{path}@{ref}: bad base64: {exc}"
            ) from exc

    def landed_at(self, *, repo: str, path: str, ref: str) -> str:
        payload = self._json(
            ["api", f"repos/{repo}/commits?sha={ref}&path={path}&per_page=1"]
        )
        if not isinstance(payload, list) or not payload:
            return ""
        newest = payload[0] if isinstance(payload[0], dict) else {}
        sha = newest.get("sha")
        commit = newest.get("commit")
        committer = commit.get("committer") if isinstance(commit, dict) else None
        date = committer.get("date") if isinstance(committer, dict) else None
        committed = date if isinstance(date, str) else ""
        if not isinstance(sha, str):
            return committed
        # The committer date is when the squash commit was BUILT, which a merge
        # queue does well before the commit reaches the branch: omnibase_infra
        # #4206's commit reads 15:38:41Z and merged to dev at 16:01:07Z. The
        # PR's merged_at is when the gate could first have seen the bytes.
        pulls = self._json(["api", f"repos/{repo}/commits/{sha}/pulls"])
        if isinstance(pulls, list):
            for pull in pulls:
                if not isinstance(pull, dict):
                    continue
                base = pull.get("base")
                merged_at = pull.get("merged_at")
                if (
                    pull.get("merge_commit_sha") == sha
                    and isinstance(base, dict)
                    and base.get("ref") == ref
                    and isinstance(merged_at, str)
                ):
                    return merged_at
        return committed

    def _write(self, args: list[str], *, token: str | None = None) -> None:
        completed = self._run(args, token=token)
        if completed.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )

    def update_branch(
        self, *, repo: str, pr_number: int, expected_head_sha: str
    ) -> None:
        if not self._update_token:
            raise RuntimeError(
                "no GH_UPDATE_TOKEN: a branch update under the workflow token "
                "starts no CI on the new head, so it is refused rather than issued"
            )
        self._write(
            [
                "api",
                "-X",
                "PUT",
                f"repos/{repo}/pulls/{pr_number}/update-branch",
                "-f",
                f"expected_head_sha={expected_head_sha}",
            ],
            token=self._update_token,
        )

    def rerun_run(self, *, repo: str, run_id: int) -> None:
        self._write(["run", "rerun", str(run_id), "--repo", repo])

    def rerun_job(self, *, repo: str, job_id: int) -> None:
        self._write(["run", "rerun", "--job", str(job_id), "--repo", repo])


# ---------------------------------------------------------------------------
# Collection (reads only)
# ---------------------------------------------------------------------------


def read_dev_release_state(gh: GhPort, *, repo: str) -> DevReleaseState | None:
    """Dev's version and the newest published tag; ``None`` when either is unreadable."""
    try:
        body = gh.file_bytes(repo=repo, path="pyproject.toml", ref=DEV_BRANCH)
        tags = gh.tag_names(repo=repo)
    except RuntimeError:
        return None
    if body is None:
        return None
    dev_version = pyproject_version(body.decode("utf-8", errors="replace"))
    latest = latest_published_version(tags)
    if dev_version is None or latest is None:
        return None
    return DevReleaseState(dev_version=dev_version, latest_published=latest)


def read_vendor_state(
    gh: GhPort, *, repo: str, infra_repo: str, head_sha: str, src_path: str
) -> MigrationVendorState:
    """One named migration's state on infra ``dev`` against this head's bytes."""
    try:
        dest = vendored_dest_path(src_path)
    except ValueError:
        return MigrationVendorState(src_path, "", EnumVendorState.UNRESOLVED)
    try:
        ours = gh.file_bytes(repo=repo, path=src_path, ref=head_sha)
        theirs = gh.file_bytes(repo=infra_repo, path=dest, ref=DEV_BRANCH)
    except RuntimeError:
        return MigrationVendorState(src_path, dest, EnumVendorState.UNRESOLVED)
    if ours is None:
        return MigrationVendorState(src_path, dest, EnumVendorState.UNRESOLVED)
    if theirs is None:
        return MigrationVendorState(src_path, dest, EnumVendorState.MISSING)
    if ours != theirs:
        return MigrationVendorState(src_path, dest, EnumVendorState.DIFFERS)
    try:
        vendored_at = gh.landed_at(repo=infra_repo, path=dest, ref=DEV_BRANCH)
    except RuntimeError:
        vendored_at = ""
    return MigrationVendorState(src_path, dest, EnumVendorState.IDENTICAL, vendored_at)


def read_ci_summary(
    gh: GhPort, *, repo: str, check_runs: list[dict[str, object]]
) -> CiSummarySnapshot | None:
    snapshot = newest_check_run(check_runs, CI_SUMMARY_CHECK)
    if snapshot is None:
        return None
    other_red = 0
    if snapshot.red and snapshot.run_id is not None:
        try:
            jobs = gh.run_jobs(repo=repo, run_id=snapshot.run_id)
        except RuntimeError:
            # Unreadable: count it as blocked, so CI Summary is left alone.
            other_red = 1
        else:
            other_red = sum(
                1
                for job in jobs
                if job.get("name") != CI_SUMMARY_CHECK
                and job.get("conclusion") in _RED_JOB_CONCLUSIONS
            )
    return CiSummarySnapshot(
        job_id=snapshot.check_run_id,
        run_id=snapshot.run_id,
        red=snapshot.red,
        other_red_jobs=other_red,
    )


@dataclass(frozen=True)
class PassDecisions:
    release_window: tuple[ReleaseWindowDecision, ...]
    vendor_parity: tuple[VendorParityDecision, ...]


def collect_decisions(
    gh: GhPort,
    *,
    repo: str,
    infra_repo: str = INFRA_REPO_DEFAULT,
    only_pr: int | None = None,
    release_window: bool = True,
    vendor_parity: bool = True,
) -> PassDecisions:
    """One decision per heal per open PR considered. Reads only."""
    dev_state: DevReleaseState | None = None
    dev_read = False
    windows: list[ReleaseWindowDecision] = []
    parities: list[VendorParityDecision] = []

    for pr in gh.open_pull_requests(repo=repo):
        if only_pr is not None and pr.number != only_pr:
            continue
        runs = gh.check_runs(repo=repo, head_sha=pr.head_sha)

        updating = False
        if release_window:
            gate = newest_check_run(runs, RELEASE_IDENTITY_CHECK)
            failed: tuple[tuple[int, ...], tuple[int, ...]] | None = None
            if gate is not None and gate.red and not pr.is_draft:
                try:
                    log = gh.job_log(repo=repo, job_id=gate.check_run_id)
                except RuntimeError:
                    log = ""
                failed = parse_release_identity_failure(log)
                if failed is not None and not dev_read:
                    dev_state = read_dev_release_state(gh, repo=repo)
                    dev_read = True
            decision = decide_release_window(
                ReleaseWindowInput(
                    pr=pr, gate=gate, failed_versions=failed, dev=dev_state
                )
            )
            windows.append(decision)
            updating = decision.act

        if vendor_parity:
            gate = newest_check_run(runs, VENDOR_PARITY_CHECK)
            migrations: tuple[MigrationVendorState, ...] = ()
            attempt = 1
            summary: CiSummarySnapshot | None = None
            if gate is not None and gate.red and not pr.is_draft and not updating:
                try:
                    named = vendor_migrations_named(
                        gh.annotations(repo=repo, check_run_id=gate.check_run_id)
                    )
                except RuntimeError:
                    named = []
                migrations = tuple(
                    read_vendor_state(
                        gh,
                        repo=repo,
                        infra_repo=infra_repo,
                        head_sha=pr.head_sha,
                        src_path=src,
                    )
                    for src in named
                )
                if migrations and all(
                    m.state is EnumVendorState.IDENTICAL for m in migrations
                ):
                    try:
                        raw_attempt = gh.job(repo=repo, job_id=gate.check_run_id).get(
                            "run_attempt", 1
                        )
                    except RuntimeError:
                        raw_attempt = MAX_HEAL_RUN_ATTEMPT
                    attempt = raw_attempt if isinstance(raw_attempt, int) else 1
                    summary = read_ci_summary(gh, repo=repo, check_runs=runs)
            parities.append(
                decide_vendor_parity(
                    VendorParityInput(
                        pr=pr,
                        gate=gate,
                        run_attempt=attempt,
                        migrations=migrations,
                        ci_summary=summary,
                        superseded_by_update=updating,
                    )
                )
            )

    return PassDecisions(release_window=tuple(windows), vendor_parity=tuple(parities))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface.

    There is deliberately no ``--force``, no ``--skip`` and no flag asserting
    dev's version, a tag or a vendor state: every precondition is read live on
    every pass. ``tests/ci/test_pr_stale_red_heal_omn19852.py`` reads these
    option strings and fails if one appears.
    """
    parser = argparse.ArgumentParser(prog="pr_stale_red_heal.py", description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name of the product repo")
    parser.add_argument(
        "--infra-repo",
        default=INFRA_REPO_DEFAULT,
        help="owner/name of the repo that vendors node migrations",
    )
    parser.add_argument(
        "--heal",
        choices=("all", "release-window", "vendor-parity"),
        default="all",
        help="which heal to run",
    )
    parser.add_argument(
        "--pr-number",
        default="",
        help="consider only this PR; empty considers every open PR",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the decisions without updating a branch or re-running anything",
    )
    return parser


def main(argv: list[str] | None = None, *, gh: GhPort | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client: GhPort = (
        gh
        if gh is not None
        else GhCli(update_token=os.environ.get("GH_UPDATE_TOKEN") or None)
    )

    only_pr: int | None = None
    raw_pr = (args.pr_number or "").strip()
    if raw_pr:
        try:
            only_pr = int(raw_pr)
        except ValueError:
            print(f"::error::--pr-number must be an integer, got {raw_pr!r}")
            return EXIT_ERROR

    try:
        decisions = collect_decisions(
            client,
            repo=args.repo,
            infra_repo=args.infra_repo,
            only_pr=only_pr,
            release_window=args.heal in ("all", "release-window"),
            vendor_parity=args.heal in ("all", "vendor-parity"),
        )
    except RuntimeError as exc:
        print(f"::error::could not resolve heal state: {exc}")
        return EXIT_ERROR

    attempted = 0
    failures: list[str] = []
    updates = reruns = 0

    for window in decisions.release_window:
        print(f"release-window {window.outcome.value}: {window.detail}")
        if not window.act or args.dry_run:
            continue
        if updates >= MAX_UPDATES_PER_PASS:
            print(f"  deferred to the next pass ({MAX_UPDATES_PER_PASS}-update cap)")
            continue
        attempted += 1
        try:
            client.update_branch(
                repo=args.repo,
                pr_number=window.pr_number,
                expected_head_sha=window.head_sha,
            )
            updates += 1
            print(
                f"  updated branch of #{window.pr_number} from {window.head_sha[:12]}"
            )
        except RuntimeError as exc:
            failures.append(f"#{window.pr_number}: {exc}")
            print(f"::warning::could not update #{window.pr_number}: {exc}")

    rerun_prs = 0
    for parity in decisions.vendor_parity:
        print(f"vendor-parity {parity.outcome.value}: {parity.detail}")
        if not parity.act or args.dry_run:
            continue
        if rerun_prs >= MAX_RERUN_PRS_PER_PASS:
            print(f"  deferred to the next pass ({MAX_RERUN_PRS_PER_PASS}-PR cap)")
            continue
        rerun_prs += 1
        # The gate first, then CI Summary: the poller then sees the gate
        # queued or running and waits for it instead of reading the old red.
        for run_id in parity.run_ids:
            attempted += 1
            try:
                client.rerun_run(repo=args.repo, run_id=run_id)
                reruns += 1
                print(f"  re-ran run {run_id}")
            except RuntimeError as exc:
                failures.append(f"run {run_id}: {exc}")
                print(f"::warning::could not re-run run {run_id}: {exc}")
        for job_id in parity.job_ids:
            attempted += 1
            try:
                client.rerun_job(repo=args.repo, job_id=job_id)
                reruns += 1
                print(f"  re-ran job {job_id}")
            except RuntimeError as exc:
                failures.append(f"job {job_id}: {exc}")
                print(f"::warning::could not re-run job {job_id}: {exc}")

    considered = max(len(decisions.release_window), len(decisions.vendor_parity))
    print(
        f"considered {considered} open PR(s); updated {updates} branch(es); "
        f"issued {reruns} re-run(s){' (dry run)' if args.dry_run else ''}"
    )
    if attempted and failures and len(failures) == attempted:
        print(f"::error::every write this pass attempted failed: {'; '.join(failures)}")
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
