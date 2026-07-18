# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-controller branch-safety gate (OMN-14768 / friction F-11 + F-12).

Two fail-closed checks that guard the branch identity of merge-controller work:

F-11 — branch-rename-on-open-PR prohibition (``rename-scan`` mode)
-----------------------------------------------------------------
Renaming an open PR's *head branch* CLOSES the PR (GitHub behaviour): the
overnight sweep renamed ``omnidash#258``'s branch to add ``OMN-14153`` and the PR
closed, forcing a replacement ``#259`` plus reminted OCC evidence. No automated
branch rename exists in the merge controller today, so this is a PREVENTIVE gate:
it scans the merge-controller node tree (and any file it is handed) and fails
closed if a branch-rename invocation is ever introduced —
``repos/.../branches/.../rename`` (REST), ``gh api ... rename``, or a local
``git branch -m/--move``. The only escape hatch is an explicit
``# branch-rename-ok: <reason>`` annotation on the exact line (reserved for a
forensic/illustrative quote, never a live rename), mirroring the
``# raw-prod-bypass-ok:`` pattern.

F-12 — early Evidence-Ticket branch-identity check (``identity`` mode)
---------------------------------------------------------------------
The Receipt Gate FAILS a PR whose branch name omits the ``Evidence-Ticket`` id
(``omnidash#258``: valid evidence, unmergeable). That is the LATE gate. This is
the EARLY (pre-push / PR-open) check: for every ``Evidence-Ticket: OMN-<n>`` in
the PR body / commit trailers, assert the branch name references the ticket —
failing with the EXACT required branch fragment. It reuses the SAME
``omnibase_core.validation.validator_receipt_gate`` regexes and branch-axis logic
(including the OMN-13395 dual-ticket ``omn-A-B`` cluster convention), so the early
check and the late gate can never drift.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Reuse the gate's OWN ticket/branch regexes so the early check and the late
# Receipt Gate can never diverge (OMN-10420 / OMN-13395). omnibase_core is a
# declared omnimarket dependency (the OCC emitter imports from the same module).
from omnibase_core.validation.validator_receipt_gate import (
    DUAL_TICKET_BRANCH_CLUSTER_PATTERN,
    EVIDENCE_TICKET_PATTERN,
    TICKET_PATTERN,
)

# ---------------------------------------------------------------------------
# F-11 — branch-rename detection
# ---------------------------------------------------------------------------

_BRANCH_RENAME_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "REST branch-rename endpoint",
        re.compile(r"repos/[^\s\"']+/branches/[^\s\"']+/rename"),
    ),
    (
        "gh api branch rename",
        re.compile(r"gh\s+api\b[^\n]*\brename\b"),
    ),
    (
        "local git branch rename (-m/--move)",
        # Tolerate BOTH the shell form (`git branch -m old new`) and the argv-list
        # form (`["git", "branch", "-m", ...]`) — the class allows quotes/commas
        # between tokens so a subprocess call is caught too.
        re.compile(r"\bgit\b['\"\s,]+branch\b['\"\s,]+(?:-m|-M|--move)\b"),
    ),
)

# Explicit, line-local escape hatch for a forensic/illustrative quote — never a
# live rename. Mirrors the `# raw-prod-bypass-ok:` annotation (OMN-13434).
_RENAME_OK_RE = re.compile(r"#\s*branch-rename-ok:")

# Default scan surface: the merge-controller node tree. A rename introduced
# anywhere here can close an open product PR.
_MERGE_CONTROLLER_GLOBS: tuple[str, ...] = (
    "src/omnimarket/nodes/node_pr_lifecycle_*/**/*.py",
    "src/omnimarket/nodes/node_dispatch_worker/**/*.py",
    "src/omnimarket/nodes/node_merge_effect/**/*.py",
)


def find_branch_rename_calls(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line_number, label, line)`` for each branch-rename invocation.

    A line carrying the ``# branch-rename-ok:`` annotation is exempt. Pure — no
    I/O — so it is directly unit-testable with a planted positive control.
    """
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _RENAME_OK_RE.search(line):
            continue
        for label, pattern in _BRANCH_RENAME_RES:
            if pattern.search(line):
                findings.append((lineno, label, line.strip()))
                break
    return findings


def _resolve_scan_paths(paths: list[Path], root: Path) -> list[Path]:
    if paths:
        return [p for p in paths if p.suffix == ".py" and p.is_file()]
    resolved: list[Path] = []
    for glob in _MERGE_CONTROLLER_GLOBS:
        resolved.extend(sorted(root.glob(glob)))
    return resolved


def run_rename_scan(paths: list[Path], root: Path) -> int:
    """Scan ``paths`` (or the merge-controller tree) for branch-rename calls."""
    scan = _resolve_scan_paths(paths, root)
    violations: list[str] = []
    for path in scan:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, label, line in find_branch_rename_calls(text):
            violations.append(f"  {path}:{lineno}: {label}\n      {line}")
    if violations:
        joined = "\n".join(violations)
        print(
            "BRANCH-RENAME PROHIBITED (OMN-14768 / F-11): renaming an open PR's "
            "head branch CLOSES the PR.\n"
            f"{len(violations)} branch-rename invocation(s) in the merge "
            f"controller:\n{joined}\n"
            "    Fix: never rename a branch on an open PR — create a new "
            "branch/PR intentionally when branch identity must change. If this is "
            "a forensic/illustrative quote, annotate the exact line with "
            "`# branch-rename-ok: <reason>`.",
            file=sys.stderr,
        )
        return 1
    print(f"branch-rename scan OK: {len(scan)} merge-controller file(s) clean")
    return 0


# ---------------------------------------------------------------------------
# F-12 — Evidence-Ticket branch-identity check
# ---------------------------------------------------------------------------


def normalize_ticket(ticket: str) -> str:
    """Return the canonical ``OMN-<n>`` form (upper-cased) of ``ticket``."""
    match = TICKET_PATTERN.search(ticket)
    if match is None:
        return ticket.strip().upper()
    return f"OMN-{match.group(1)}"


def branch_binds_ticket(branch_name: str, ticket: str) -> bool:
    """True when ``branch_name`` references ``ticket`` (Receipt-Gate branch axis).

    Byte-identical to ``verify_ticket_identity``'s branch axis
    (validator_receipt_gate): an exact ``OMN-<n>`` token, OR — for the OMN-13395
    dual-ticket ``omn-A-B(-C...)`` convention — the ticket number appearing in a
    branch cluster run. Case-insensitive.
    """
    ticket_upper = normalize_ticket(ticket).upper()
    exact_ticket_pattern = re.compile(
        rf"(?<![A-Z0-9]){re.escape(ticket_upper)}(?![A-Z0-9])"
    )
    branch_upper = branch_name.upper()
    if exact_ticket_pattern.search(branch_upper) is not None:
        return True
    ticket_number = re.search(r"(\d+)$", ticket_upper)
    if ticket_number is not None:
        target = ticket_number.group(1)
        return any(
            target in cluster.split("-")
            for cluster in DUAL_TICKET_BRANCH_CLUSTER_PATTERN.findall(branch_upper)
        )
    return False


def required_branch_hint(ticket: str) -> str:
    """The exact fragment the branch must contain (e.g. ``omn-14766``)."""
    return normalize_ticket(ticket).lower()


def extract_evidence_tickets(text: str) -> list[str]:
    """Return the ``Evidence-Ticket: OMN-<n>`` ids in ``text`` (PR body/commits).

    Matches the Receipt Gate's ``EVIDENCE_TICKET_PATTERN`` line-by-line, so the
    early check binds the exact same ids the late gate will. Order-preserving,
    de-duplicated.
    """
    seen: dict[str, None] = {}
    for line in text.splitlines():
        match = EVIDENCE_TICKET_PATTERN.match(line.strip())
        if match is not None:
            seen[normalize_ticket(match.group(1))] = None
    return list(seen.keys())


def _current_branch(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch or None


def _git_range_messages(root: Path, base: str) -> str:
    try:
        result = subprocess.run(
            ["git", "log", "--format=%B", f"{base}..HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def run_identity_check(*, branch: str | None, evidence_text: str) -> int:
    """Assert ``branch`` references every Evidence-Ticket in ``evidence_text``."""
    tickets = extract_evidence_tickets(evidence_text)
    if not tickets:
        # No Evidence-Ticket to bind — the Receipt Gate governs the missing case.
        print("branch-identity OK: no Evidence-Ticket to bind")
        return 0
    if not branch:
        print(
            "branch-identity: could not resolve the branch name to validate.",
            file=sys.stderr,
        )
        return 1

    mismatches = [t for t in tickets if not branch_binds_ticket(branch, t)]
    if mismatches:
        hints = ", ".join(sorted(f"'{required_branch_hint(t)}'" for t in mismatches))
        print(
            f"IDENTITY BINDING FAILED (OMN-14768 / F-12): branch {branch!r} does "
            f"not reference Evidence-Ticket(s) {', '.join(sorted(mismatches))}.\n"
            f"    The branch name must contain {hints} (the Receipt Gate branch "
            f"axis rejects it otherwise — the omnidash#258 late-failure class).\n"
            f"    Fix: renaming an open PR's branch closes it (F-11); create the "
            f"branch/PR with the ticket in its name, or add the Evidence-Ticket "
            f"that matches the branch.",
            file=sys.stderr,
        )
        return 1
    print(f"branch-identity OK: {branch!r} references {', '.join(sorted(tickets))}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--mode",
        choices=("rename-scan", "identity"),
        default="rename-scan",
        help="rename-scan: F-11 branch-rename prohibition; identity: F-12 check.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="rename-scan: files to scan (default: merge-controller tree).",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--branch", help="identity: branch name (default: git HEAD).")
    parser.add_argument(
        "--body-file", type=Path, help="identity: PR body / evidence text file."
    )
    parser.add_argument(
        "--from-git-base",
        help="identity: read Evidence-Ticket from commit messages in "
        "<base>..HEAD (e.g. origin/dev).",
    )
    args = parser.parse_args(argv)

    if args.mode == "rename-scan":
        return run_rename_scan(args.paths, args.root)

    branch = args.branch or _current_branch(args.root)
    evidence_text = ""
    if args.body_file is not None and args.body_file.is_file():
        evidence_text = args.body_file.read_text(encoding="utf-8")
    elif args.from_git_base:
        evidence_text = _git_range_messages(args.root, args.from_git_base)
    return run_identity_check(branch=branch, evidence_text=evidence_text)


if __name__ == "__main__":
    sys.exit(main())
