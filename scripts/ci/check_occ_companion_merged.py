# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC companion-merged gate (OMN-15214, ported to omnimarket per OMN-15427) — a
strict ``CI Summary`` gate.

Why this exists
---------------
On 2026-07-26 an automated "OCC queue hygiene" pass closed five OPEN
onex_change_control evidence companions (#5012-#5016) whose product PRs had
already MERGED, destroying three evidence chains with no successor. The sweep's
trigger state is exactly "OPEN companion + MERGED product PR".

omnimarket was the last CI-repo on the fleet without this gate, and the gap was
observed live: ``omnimarket#1953``'s body cited ``Evidence-Source: OCC#5487``, a
companion that had been **CLOSED without merging** (superseded by the merged
OCC#5497). The canonical arbiter, run by hand against #1953, returned FAIL — but
omnimarket's CI never ran it, so the PR sat green-looking with a dead evidence
citation and nothing noticed (OMN-15427).

This gate makes that state unreachable at the merge boundary for this repo:
a product PR's ``CI Summary`` (a required branch-protection context on
omnimarket ``dev`` and ``main``) cannot go green until **every** OCC evidence
citation in the PR's body is DURABLE — i.e. each cited companion PR is MERGED,
or each cited commit SHA is an ancestor of an onex_change_control durable branch
(dev/main). Because the product PR cannot merge before its companions do,
"merged product + open companion" can no longer arise via the merge path, and a
companion-closing sweep has nothing load-bearing to destroy.

Every citation, not just the first
----------------------------------
The omniclaude port (OMN-15221) evaluated only the FIRST ``Evidence-Source:``
line. omnimarket PR bodies routinely carry more than one companion citation
(re-binds, split evidence, ``fix(...)``-style re-cuts such as the #1953 →
OCC#5487 → OCC#5497 → OCC#5506 chain), so a first-line-only check is trivially
evadable: append a fresh merged citation above a dead one and the gate greens on
evidence that no longer exists. This port evaluates **all** citations and
aggregates fail-closed (any FAIL ⇒ FAIL; else any PENDING ⇒ PENDING).

Parse like the arbiter binds (OMN-15475)
----------------------------------------
"All citations" is only safe if it is a SUPERSET of what the canonical arbiter
binds. It was not: this gate stripped fenced blocks before building the entire
citation set, so a fenced dead example sitting **above** a real stamp was erased
from its view while ``occ-preflight.yml``/``receipt-gate.yml`` — which grep the
**raw** body and take ``head -1`` — bound that dead ref and pinned its branch
head. The two surfaces disagreed field-by-field on what "the citation" is, in
silence (the OMN-14208 seam class), and since occ-preflight deliberately
*accepts* a non-MERGED companion by falling back to ``headRefOid``, this gate is
the sole detector for the dead-citation class. The evaluated set is now the
UNION of the fence-filtered set and :func:`canonical_first_citation` — a faithful
reproduction of the arbiter's pipeline, POSIX space class and ``\n``-only line
splitting included.

Deliberately NOT a new required status check: this job is registered in
:data:`scripts.ci.ci_summary_gate.STRICT_GATE_JOBS` (present + completed +
conclusion ``success``; a skip/cancel/absence fails closed) and enforced through
the existing fail-closed ``CI Summary`` umbrella poller. Adding a new top-level
required context that does not report on every PR shape wedges merges
indefinitely (see CLAUDE.md, deploy-gate section); the umbrella pattern has no
such failure mode because its check-run always instantiates.

Verdict model (mirrors ci_summary_gate exit codes)
--------------------------------------------------
* ``PASS`` (0)    — every citation is durable, or the gate does not apply
  (non-PR event; trusted dependency-bot author, mirroring occ-preflight's
  OMN-13762 exemption).
* ``PENDING`` (2) — evidence may still become durable without a new commit:
  Evidence-Source not yet PATCHed onto the body by occ-autobind, a companion
  still OPEN (auto-merge in flight), or a transient API error. The runner
  entrypoint polls; at the deadline PENDING converts to FAIL (fail-closed).
* ``FAIL`` (1)    — evidence can never become durable in this state:
  a companion CLOSED without merging (the incident state, and the #1953 shape),
  a cited SHA that is not an ancestor of an OCC durable branch (squash-only
  merges guarantee a feature-branch head SHA never becomes one — the OMN-15216
  defect), or a malformed Evidence-Source value.

The companion-must-merge-first ordering is safe: onex_change_control PRs have
no reverse dependency on product-PR merge state (occ-preflight validates OCC's
own PRs from their in-tree diff), and repo-level auto-merge is enabled there.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # fixed argv, no shell, trusted gh binary
import sys
import time
from dataclasses import dataclass

OCC_REPO_DEFAULT = "OmniNode-ai/onex_change_control"

# Branches on which an OCC commit SHA counts as durable evidence.
OCC_DURABLE_BRANCHES: tuple[str, ...] = ("dev", "main")

# Mirrors occ-preflight's OMN-13762 dependency-bot exemption
# (validator_receipt_gate.DEPENDENCY_BOT_AUTHORS): bot-authored dependency
# bumps structurally cannot cite OCC evidence.
DEPENDENCY_BOT_AUTHORS: frozenset[str] = frozenset(
    {
        "dependabot[bot]",
        "app/dependabot",
        "dependabot",
        "renovate[bot]",
        "app/renovate",
        "renovate",
    }
)

# Events on which the gate enforces (mirrors occ-preflight's event scope).
ENFORCED_EVENTS: frozenset[str] = frozenset({"pull_request", "merge_group"})

# A citation is a body line that STARTS, at column 0, with the
# ``Evidence-Source:`` trailer. This is a deliberate byte-for-byte seam match
# with the two surfaces that already define "citation" for this fleet — the
# ``Resolve Evidence-Source`` steps of ``occ-preflight.yml`` and
# ``receipt-gate.yml``, both of which extract with:
#
#     grep -iE '^Evidence-Source:[[:space:]]+\S'
#
# Do NOT widen this to tolerate list bullets (``- Evidence-Source: ...``) or
# bold wrappers (``**Evidence-Source**: ...``). An earlier revision of this port
# did, and that made omnimarket the only repo on the fleet whose citation set
# disagreed with occ-preflight's: a body that merely *documents* a dead
# companion in a bullet (``- **Evidence-Source**: OCC#5487 (superseded)``)
# became a live citation here and red the required ``CI Summary`` umbrella for
# the full 1500s poll, while occ-preflight did not treat that line as a stamp at
# all and would still fail the PR for having no stamp. The widening also closed
# no evasion vector: a bullet-only stamp can never be a PR's real evidence claim
# because occ-preflight rejects the PR outright for missing a canonical stamp.
# Seam rule: one definition of "citation", owned by the canonical grep above.
#
# Inline occurrences (``see `Evidence-Source: OCC#1` above``) are likewise NOT
# citations — the marker is not the leading content of the line.
EVIDENCE_SOURCE_RE = re.compile(
    r"^Evidence-Source:[ \t]+(\S.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# Fenced code blocks (``` or ~~~) are excluded from the *reported* citation set.
# A PR body that *explains* this gate — including this port's own RED/GREEN
# proof — necessarily quotes dead citations, and reporting every quoted example
# as a live citation would red PRs for describing the mechanism.
#
# Fence-stripping is a REPORTING filter only. It is NOT allowed to remove the
# citation the canonical arbiter actually binds — see
# :func:`canonical_first_citation` and OMN-15475. The prior revision of this
# port stripped fences before building the ENTIRE citation set, and argued the
# divergence was "fail-closed in aggregate". That argument covered exactly one
# shape (a body whose ONLY stamp is fenced ⇒ zero citations ⇒ PENDING ⇒ FAIL at
# the deadline) and was false for the shape that matters:
#
#     Example of a dead citation:
#
#     <fence>
#     Evidence-Source: OCC#5487      <- CLOSED unmerged, column 0, inside a fence
#     <fence>
#
#     Evidence-Source: OCC#5548      <- MERGED
#
# occ-preflight/receipt-gate grep the RAW body and take ``head -1``, so they
# bind the dead ``OCC#5487`` and pin its branch head (occ-preflight deliberately
# falls back to ``headRefOid`` for a non-MERGED companion — it ACCEPTS a dead
# one). The gate, seeing only ``OCC#5548``, greened. That is the omnimarket#1953
# originating incident recurring straight through the gate ported to prevent it,
# and this gate is the sole detector for the class.
#
# Safe idiom for documenting a dead citation in a PR body: indent the example by
# one space. An indented ``Evidence-Source:`` line is invisible to BOTH surfaces
# (the canonical grep anchors ``^Evidence-Source:`` at column 0, as does
# :data:`EVIDENCE_SOURCE_RE`), so it is neither bound nor evaluated — with or
# without a fence around it.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")

# Faithful reproduction of the canonical binder that occ-preflight.yml and
# receipt-gate.yml actually run against the RAW body:
#
#     printf '%s' "$pr_body" \
#       | grep -iE '^Evidence-Source:[[:space:]]+\S' \
#       | head -1 \
#       | sed -E 's/^Evidence-Source:[[:space:]]*//; s/^[[:space:]]+//; s/[[:space:]]+$//'
#
# Three fidelity details that :data:`EVIDENCE_SOURCE_RE` gets wrong, two of which
# produced live wrong-binding shapes (OMN-15475):
#
# 1. POSIX ``[[:space:]]`` inside a grep line is ``[ \t\v\f\r]`` — a STRICT
#    SUPERSET of Python's ``[ \t]``. ``Evidence-Source:\x0cOCC#5487`` is a
#    citation to the arbiter and was not one here.
# 2. ``grep`` splits lines on ``\n`` ONLY. Python's ``str.splitlines()`` also
#    splits on ``\v``, ``\f``, ``\x1c``-``\x1e``, ``\x85`` and the Unicode
#    line/paragraph separators — so ``splitlines()`` manufactures line breaks
#    the arbiter never sees. This module therefore splits on ``"\n"`` for the
#    canonical binder.
# 3. The pipeline's ``grep -i`` is case-INSENSITIVE but its ``sed -E`` is NOT,
#    so a lowercase ``evidence-source: OCC#1`` line is SELECTED and then left
#    un-stripped, yielding the literal value ``evidence-source: OCC#1`` — which
#    occ-preflight then hard-rejects as "not a valid OCC#<number> or hex SHA".
#    :func:`canonical_first_citation` reproduces that byte-for-byte (the parity
#    oracle asserts it against real ``grep``/``sed``);
#    :func:`_normalize_canonical_value` then re-strips the trailer
#    case-insensitively so this gate evaluates the companion the author meant
#    rather than emitting a malformed-value FAIL for a shape the arbiter already
#    fails on its own. No dead ref can hide in this shape: the arbiter pins
#    nothing here, it refuses the PR outright.
_POSIX_SPACE_CHARS = " \t\v\f\r"
# Line SELECTION mirrors `grep -iE` — case-insensitive.
_CANONICAL_SELECT_RE = re.compile(
    r"^Evidence-Source:[ \t\v\f\r]+\S",
    re.IGNORECASE,
)
# Value EXTRACTION mirrors `sed -E 's/^Evidence-Source:[[:space:]]*//'` — case
# SENSITIVE, deliberately. Do not add re.IGNORECASE here: that would make the
# binder disagree with the arbiter, which is the whole defect being fixed.
_CANONICAL_SED_RE = re.compile(r"^Evidence-Source:[ \t\v\f\r]*")
# Our own post-hoc normalization of the residue above (see fidelity note 3).
_RESIDUAL_TRAILER_RE = re.compile(
    r"^Evidence-Source:[ \t\v\f\r]*",
    re.IGNORECASE,
)
OCC_PR_REF_RE = re.compile(r"^OCC#(\d+)$", re.IGNORECASE)
HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
MERGE_GROUP_PR_RE = re.compile(r"/pr-(\d+)-")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PENDING = 2

_VERDICT_NAMES = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_PENDING: "PENDING"}


@dataclass(frozen=True)
class Verdict:
    """Terminal or poll-again outcome of a single gate evaluation."""

    code: int  # EXIT_PASS | EXIT_FAIL | EXIT_PENDING
    reason: str

    @property
    def name(self) -> str:
        return _VERDICT_NAMES[self.code]


class GhFetcher:
    """Live GitHub reads via the ``gh`` CLI. Every failure returns ``None``
    so the caller decides between PENDING (retryable) and FAIL (terminal)."""

    def _run(self, argv: list[str]) -> str | None:
        try:
            result = subprocess.run(  # fixed argv, no shell
                argv, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"::warning::gh invocation failed: {exc}", file=sys.stderr)
            return None
        if result.returncode != 0:
            print(
                f"::warning::{' '.join(argv[:4])}... exited "
                f"{result.returncode}: {result.stderr.strip()[:300]}",
                file=sys.stderr,
            )
            return None
        return result.stdout

    def pr_view(self, repo: str, number: str, fields: str) -> dict[str, object] | None:
        raw = self._run(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", fields]
        )
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def compare_status(self, repo: str, base: str, head_sha: str) -> str | None:
        """``identical``/``behind`` ⇒ ``head_sha`` is an ancestor of ``base``."""
        raw = self._run(
            [
                "gh",
                "api",
                f"repos/{repo}/compare/{base}...{head_sha}",
                "--jq",
                ".status",
            ]
        )
        return raw.strip() if raw is not None else None


def strip_fenced_blocks(body: str) -> str:
    """Blank out fenced code blocks, preserving line numbering."""

    lines = (body or "").splitlines()
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def canonical_first_citation(body: str) -> str | None:
    """The citation ``occ-preflight.yml``/``receipt-gate.yml`` ACTUALLY bind.

    Reproduces their ``grep -iE '^Evidence-Source:[[:space:]]+\\S' | head -1``
    + ``sed`` pipeline against the RAW body: no fence-stripping, POSIX space
    class, ``\\n``-only line splitting. Returns ``None`` when the arbiter would
    resolve nothing (it then fails the PR for a missing stamp).

    This is the seam anchor for OMN-15475: whatever this returns is the ref the
    arbiter pins, so it MUST be inside the evaluated citation set regardless of
    fences or whitespace class.
    """

    for line in (body or "").split("\n"):
        if _CANONICAL_SELECT_RE.match(line):
            return _CANONICAL_SED_RE.sub("", line, count=1).strip(_POSIX_SPACE_CHARS)
    return None


def _normalize_canonical_value(value: str) -> str:
    """Undo the arbiter's case-sensitive-``sed`` residue (see fidelity note 3)."""

    return _RESIDUAL_TRAILER_RE.sub("", value).strip(_POSIX_SPACE_CHARS)


def parse_evidence_sources(body: str) -> list[str]:
    """Every citation this gate evaluates, de-duplicated, canonical binding first.

    The set is the UNION of:

    * :func:`canonical_first_citation` — the ONE value occ-preflight/receipt-gate
      bind from the raw body. Never droppable: dropping it is what let a fenced
      dead example above a real stamp green this gate while the arbiter pinned
      the dead ref (OMN-15475).
    * every column-0 ``Evidence-Source:`` line OUTSIDE a fenced block — because
      a first-match-only parse is evadable: appending a fresh merged citation
      above a dead one would green the gate on destroyed evidence.

    Fences still filter the second set (documentation stays documentation); they
    can no longer filter the first.
    """

    seen: set[str] = set()
    values: list[str] = []

    def _add(value: str) -> None:
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        values.append(value)

    canonical = canonical_first_citation(body)
    if canonical is not None:
        _add(_normalize_canonical_value(canonical))

    for match in EVIDENCE_SOURCE_RE.finditer(strip_fenced_blocks(body)):
        _add(match.group(1).strip())
    return values


def resolve_pr_number(
    event_name: str, pr_number: str, merge_group_head_ref: str
) -> str:
    """PR number for pull_request or merge_group events ('' if unresolvable)."""
    if pr_number:
        return pr_number
    if event_name == "merge_group" and merge_group_head_ref:
        match = MERGE_GROUP_PR_RE.search(merge_group_head_ref)
        if match:
            return match.group(1)
    return ""


def evaluate_citation(
    fetcher: GhFetcher, evidence_source: str, *, occ_repo: str = OCC_REPO_DEFAULT
) -> Verdict:
    """Durability verdict for ONE ``Evidence-Source`` value."""

    occ_ref = OCC_PR_REF_RE.match(evidence_source)
    if occ_ref:
        occ_pr = occ_ref.group(1)
        occ_data = fetcher.pr_view(occ_repo, occ_pr, "state,mergeCommit")
        if occ_data is None:
            return Verdict(
                EXIT_PENDING,
                f"could not fetch companion {occ_repo}#{occ_pr} (retryable)",
            )
        state = str(occ_data.get("state") or "").upper()
        if state == "MERGED":
            merge_commit = occ_data.get("mergeCommit")
            merge_oid = ""
            if isinstance(merge_commit, dict):
                merge_oid = str(merge_commit.get("oid") or "")
            return Verdict(
                EXIT_PASS,
                f"companion OCC#{occ_pr} is MERGED "
                f"(merge commit {merge_oid or 'unknown'}) — evidence is durable",
            )
        if state == "OPEN":
            return Verdict(
                EXIT_PENDING,
                f"companion OCC#{occ_pr} is still OPEN — the companion must MERGE "
                "before this product PR may merge (OMN-15214). Land the companion "
                "on onex_change_control, then re-run this job.",
            )
        # CLOSED without merging: the exact state the 2026-07-26 hygiene sweep
        # minted, and the omnimarket#1953 / OCC#5487 shape. Never poll; fail loudly.
        return Verdict(
            EXIT_FAIL,
            f"companion OCC#{occ_pr} is {state or 'UNRESOLVED'} without merging — "
            "the cited evidence no longer exists. Re-cut the companion (bind it to "
            "this PR) and update Evidence-Source before merging.",
        )

    if HEX_SHA_RE.match(evidence_source.lower()):
        sha = evidence_source.lower()
        saw_api_error = False
        for branch in OCC_DURABLE_BRANCHES:
            status = fetcher.compare_status(occ_repo, branch, sha)
            if status is None:
                saw_api_error = True
                continue
            if status in ("identical", "behind"):
                return Verdict(
                    EXIT_PASS,
                    f"Evidence-Source SHA {sha} is an ancestor of {occ_repo}@{branch} "
                    "— evidence is durable",
                )
        if saw_api_error:
            return Verdict(
                EXIT_PENDING,
                f"could not resolve Evidence-Source SHA {sha} against "
                f"{occ_repo} durable branches (retryable)",
            )
        # onex_change_control is squash-only: a feature-branch head SHA can
        # NEVER become an ancestor of dev/main, so this is terminal — it is the
        # strandable pre-merge pin OMN-15216 describes.
        return Verdict(
            EXIT_FAIL,
            f"Evidence-Source SHA {sha} is not an ancestor of any durable "
            f"{occ_repo} branch {OCC_DURABLE_BRANCHES} — cite 'OCC#<pr>' (which "
            "must be MERGED) or a merged OCC commit SHA, never a feature-branch head.",
        )

    return Verdict(
        EXIT_FAIL,
        f"Evidence-Source value '{evidence_source}' is neither 'OCC#<number>' nor a "
        "hex commit SHA — fix the PR body.",
    )


def aggregate(verdicts: list[Verdict]) -> Verdict:
    """Fail-closed rollup over per-citation verdicts.

    Any FAIL is terminal and wins (a destroyed companion can never become
    durable, so waiting is pointless). Otherwise any PENDING keeps the poll
    alive. Only an all-PASS set greens.
    """

    if not verdicts:
        return Verdict(EXIT_FAIL, "no citations evaluated — failing closed")
    failures = [v for v in verdicts if v.code == EXIT_FAIL]
    if failures:
        return Verdict(EXIT_FAIL, "; ".join(v.reason for v in failures))
    pendings = [v for v in verdicts if v.code == EXIT_PENDING]
    if pendings:
        return Verdict(EXIT_PENDING, "; ".join(v.reason for v in pendings))
    return Verdict(EXIT_PASS, "; ".join(v.reason for v in verdicts))


def evaluate_once(
    fetcher: GhFetcher,
    *,
    event_name: str,
    repo: str,
    pr_number: str,
    occ_repo: str = OCC_REPO_DEFAULT,
    evidence_source_override: list[str] | None = None,
) -> Verdict:
    """One poll iteration. PENDING means the state may still resolve itself
    (poll again); FAIL means it never can (terminal)."""

    if event_name not in ENFORCED_EVENTS:
        return Verdict(
            EXIT_PASS,
            f"event '{event_name}' is not a merge-gating event; gate not applicable",
        )

    if not pr_number:
        return Verdict(
            EXIT_FAIL,
            "could not resolve a PR number for this run — failing closed",
        )

    if evidence_source_override is None:
        # Live body, never the event payload: occ-autobind PATCHes
        # Evidence-Source onto the body AFTER the triggering event fired.
        pr_data = fetcher.pr_view(repo, pr_number, "body,author")
        if pr_data is None:
            return Verdict(
                EXIT_PENDING, f"could not fetch {repo}#{pr_number} (retryable)"
            )

        author_raw = pr_data.get("author")
        author = ""
        if isinstance(author_raw, dict):
            author = str(author_raw.get("login") or "")
        if author in DEPENDENCY_BOT_AUTHORS:
            return Verdict(
                EXIT_PASS,
                f"trusted dependency-bot author '{author}' — occ-preflight OMN-13762 "
                "exemption mirrored; no OCC evidence applicable",
            )

        evidence_sources = parse_evidence_sources(str(pr_data.get("body") or ""))
    else:
        evidence_sources = list(evidence_source_override)

    if not evidence_sources:
        return Verdict(
            EXIT_PENDING,
            f"{repo}#{pr_number} body has no 'Evidence-Source:' line yet "
            "(occ-autobind mint may still be in flight)",
        )

    return aggregate(
        [
            evaluate_citation(fetcher, source, occ_repo=occ_repo)
            for source in evidence_sources
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GH_REPO", ""))
    parser.add_argument("--pr-number", default=os.environ.get("PR_NUMBER", ""))
    parser.add_argument(
        "--event-name", default=os.environ.get("GITHUB_EVENT_NAME", "pull_request")
    )
    parser.add_argument(
        "--merge-group-head-ref", default=os.environ.get("MERGE_GROUP_HEAD_REF", "")
    )
    parser.add_argument(
        "--occ-repo", default=os.environ.get("OCC_REPO", OCC_REPO_DEFAULT)
    )
    parser.add_argument(
        "--evidence-source",
        action="append",
        default=None,
        help="Override: evaluate this Evidence-Source value directly instead of "
        "reading the PR body (diagnostics / dry-run). Repeatable.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single evaluation, no polling; exits 0/1/2 (PASS/FAIL/PENDING).",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=int(os.environ.get("DEADLINE_SECONDS", "1500")),
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "30")),
    )
    args = parser.parse_args(argv)

    pr_number = resolve_pr_number(
        args.event_name, args.pr_number, args.merge_group_head_ref
    )
    fetcher = GhFetcher()
    deadline = time.monotonic() + args.deadline_seconds

    while True:
        verdict = evaluate_once(
            fetcher,
            event_name=args.event_name,
            repo=args.repo,
            pr_number=pr_number,
            occ_repo=args.occ_repo,
            evidence_source_override=args.evidence_source,
        )
        print(f"occ-companion-merged gate: {verdict.name} — {verdict.reason}")

        if verdict.code != EXIT_PENDING or args.once:
            if verdict.code == EXIT_FAIL:
                print(f"::error::{verdict.reason}")
            return verdict.code

        if time.monotonic() >= deadline:
            print(
                f"::error::occ-companion-merged gate: poll deadline "
                f"({args.deadline_seconds}s) reached while still PENDING — failing "
                f"closed. Last state: {verdict.reason}"
            )
            return EXIT_FAIL

        time.sleep(args.poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
