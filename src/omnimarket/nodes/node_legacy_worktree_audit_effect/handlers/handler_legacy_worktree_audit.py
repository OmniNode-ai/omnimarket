"""Per-worktree audit ledger (OMN-19740), hosted by node_legacy_worktree_audit_effect.

Classifies every worktree under a given root directory into one of:

  SAFE_REMOVE_MERGED      clean tree, branch's PR is MERGED -> safe delete
  SAFE_REMOVE_GONE        clean tree, no upstream branch on remote, 0 unique commits vs origin/main
  PUSH_THEN_REMOVE        clean tree, has unpushed commits, branch tracks an upstream
  PUSH_NEW_BRANCH         clean tree, has unique commits but no remote branch (orphan local commits)
  COMMIT_AND_REVIEW       dirty working tree (uncommitted changes)
  DETACHED_REVIEW         detached HEAD (manual review required)
  UNCERTAIN               anything that didn't fit the above (broken pointer, missing canonical)
  ACTIVE_PR_LEAVE         open PR, leave worktree alone

Usage:
  onex-legacy-worktree-audit --root <worktrees-dir> --out-dir <dir>

Ticket: OMN-19740.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..models.model_legacy_worktree_audit_request import ModelLegacyWorktreeAuditRequest
from ..models.model_legacy_worktree_audit_result import ModelLegacyWorktreeAuditResult


def run(
    cmd: Sequence[str], cwd: str | None = None, timeout: int = 30
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def remote_to_slug(url: str) -> str:
    if not url:
        return ""
    url = url.removesuffix(".git")
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:") :]
    if url.startswith("https://github.com/"):
        return url[len("https://github.com/") :]
    return ""


def gather_pr_index(repo_slugs: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Return {repo_slug: {branch: {state, number}}} for OPEN+CLOSED+MERGED PRs."""
    idx: dict[str, dict[str, dict[str, Any]]] = {}
    for slug in sorted(repo_slugs):
        if not slug:
            continue
        idx[slug] = {}
        rc, out, err = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--state",
                "all",
                "--limit",
                "2000",
                "--json",
                "number,headRefName,state,mergedAt,closedAt",
            ],
            timeout=120,
        )
        if rc != 0:
            print(f"  gh pr list failed for {slug}: {err}", file=sys.stderr)
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        for pr in data:
            br = pr.get("headRefName")
            if not br:
                continue
            # last write wins; PRs with same head are rare and most-recent is usually canonical
            idx[slug][br] = {
                "number": pr.get("number"),
                "state": pr.get("state"),
                "merged_at": pr.get("mergedAt"),
                "closed_at": pr.get("closedAt"),
            }
    return idx


def audit_worktree(git_pointer: Path, root: Path) -> dict[str, Any]:
    """Audit one worktree, return a dict with state + classification."""
    wt = git_pointer.parent
    rec: dict[str, Any] = {
        "path": str(wt),
        "ticket_dir": wt.parent.name if wt.parent != root else None,
        "repo_dir_name": wt.name,
        "branch": None,
        "detached": False,
        "remote_url": None,
        "repo_slug": None,
        "upstream": None,
        "dirty_files": 0,
        "dirty_sample": [],
        "ahead": 0,
        "behind": 0,
        "unique_vs_main": 0,
        "remote_branch_exists": None,
        "pr_state": None,
        "pr_number": None,
        "canonical_dir": None,
        "errors": [],
    }

    # Resolve canonical clone
    rc, out, _ = run(["git", "-C", str(wt), "rev-parse", "--git-common-dir"])
    canonical_gitdir = Path(out) if rc == 0 and out else None
    if (
        canonical_gitdir
        and canonical_gitdir.is_absolute()
        and canonical_gitdir.exists()
    ):
        rec["canonical_dir"] = str(
            canonical_gitdir.parent
            if canonical_gitdir.name == ".git"
            else canonical_gitdir.parent.parent
            if canonical_gitdir.parent.name == ".git"
            else canonical_gitdir.parent
        )
    elif not canonical_gitdir:
        rec["errors"].append("no git-common-dir")

    # Branch
    rc, out, _ = run(
        ["git", "-C", str(wt), "symbolic-ref", "--quiet", "--short", "HEAD"]
    )
    if rc == 0 and out:
        rec["branch"] = out
    else:
        rec["detached"] = True
        rc2, out2, _ = run(["git", "-C", str(wt), "rev-parse", "--short", "HEAD"])
        rec["branch"] = f"DETACHED@{out2}" if rc2 == 0 else "DETACHED@?"

    # Remote
    rc, out, _ = run(["git", "-C", str(wt), "remote", "get-url", "origin"])
    if rc == 0 and out:
        rec["remote_url"] = out
        rec["repo_slug"] = remote_to_slug(out)

    # Upstream
    if not rec["detached"]:
        rc, out, _ = run(
            [
                "git",
                "-C",
                str(wt),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            ]
        )
        if rc == 0 and out:
            rec["upstream"] = out

    # Dirty files
    rc, out, _ = run(["git", "-C", str(wt), "status", "--porcelain=v1"])
    if rc == 0:
        lines = [ln for ln in out.split("\n") if ln.strip()]
        rec["dirty_files"] = len(lines)
        rec["dirty_sample"] = lines[:5]

    # Ahead/behind vs upstream
    if rec["upstream"]:
        rc, out, _ = run(
            [
                "git",
                "-C",
                str(wt),
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...{rec['upstream']}",
            ]
        )
        if rc == 0 and out:
            parts = out.split()
            if len(parts) == 2:
                rec["ahead"], rec["behind"] = int(parts[0]), int(parts[1])

    # Unique commits vs origin/main (best-effort)
    if not rec["detached"] and rec["branch"]:
        # Try origin/main, fall back to origin/master
        for ref in ("origin/main", "origin/master"):
            rc, out, _ = run(
                ["git", "-C", str(wt), "rev-list", "--count", f"{ref}..HEAD"]
            )
            if rc == 0 and out.isdigit():
                rec["unique_vs_main"] = int(out)
                break

    # Does the remote branch exist?
    if not rec["detached"] and rec["branch"] and rec["repo_slug"]:
        rc, out, _ = run(
            ["git", "-C", str(wt), "ls-remote", "--heads", "origin", rec["branch"]]
        )
        rec["remote_branch_exists"] = bool(out.strip()) if rc == 0 else None

    return rec


def classify(rec: dict[str, Any], pr_idx: dict[str, dict[str, dict[str, Any]]]) -> str:
    if rec["errors"]:
        return "UNCERTAIN"
    if rec["detached"]:
        # Detached HEAD: only safe to remove if 0 unique-vs-main, but that check is
        # unreliable when detached
        return "DETACHED_REVIEW"
    if rec["dirty_files"] > 0:
        return "COMMIT_AND_REVIEW"

    # Pull PR state
    branch = rec["branch"]
    slug = rec["repo_slug"]
    pr = pr_idx.get(slug, {}).get(branch) if slug and branch else None
    if pr:
        rec["pr_state"] = pr["state"]
        rec["pr_number"] = pr["number"]

    # Clean tree below
    if pr and pr["state"] == "MERGED":
        return "SAFE_REMOVE_MERGED"

    if rec["ahead"] > 0:
        return "PUSH_THEN_REMOVE"

    if rec["upstream"] is None:
        # No upstream: classify by whether the branch has unique commits vs main
        if rec["unique_vs_main"] > 0:
            return "PUSH_NEW_BRANCH"
        # No upstream, no unique commits — branch is essentially main, safe to remove
        return "SAFE_REMOVE_GONE"

    # Has upstream, ahead==0
    if rec["remote_branch_exists"] is False:
        # Remote branch deleted; safe if no unique commits vs main
        if rec["unique_vs_main"] == 0:
            return "SAFE_REMOVE_GONE"
        return "PUSH_NEW_BRANCH"

    # Has upstream, remote exists, no unpushed
    # If PR is closed-not-merged and no unique vs main, safe; if open, leave alone
    if pr and pr["state"] == "CLOSED":
        return "SAFE_REMOVE_GONE" if rec["unique_vs_main"] == 0 else "PUSH_NEW_BRANCH"
    if pr and pr["state"] == "OPEN":
        return "ACTIVE_PR_LEAVE"
    # No PR found, has remote, no unpushed, no unique vs main
    if rec["unique_vs_main"] == 0:
        return "SAFE_REMOVE_GONE"
    return "PUSH_NEW_BRANCH"


class LegacyWorktreeAuditError(RuntimeError):
    """The audit cannot run, such as a root that is not a directory."""


class HandlerLegacyWorktreeAudit:
    """Scan every worktree under a root, classify it and write the JSON and markdown ledgers."""

    def handle(
        self, request: ModelLegacyWorktreeAuditRequest
    ) -> ModelLegacyWorktreeAuditResult:
        root = request.root
        ledger_json = request.out_dir / "legacy-worktree-ledger.json"
        ledger_md = request.out_dir / "legacy-worktree-ledger.md"
        if not root.is_dir():
            raise LegacyWorktreeAuditError(f"Root does not exist: {root}")

        print(f"Scanning {root}...", file=sys.stderr)
        git_pointers = sorted(root.glob("*/*/.git")) + sorted(root.glob("*/*/*/.git"))
        # Dedupe (in case a 3-level path also matched 2-level glob)
        seen: set[Path] = set()
        pointers: list[Path] = []
        for p in git_pointers:
            if p in seen:
                continue
            seen.add(p)
            pointers.append(p)
        print(f"Found {len(pointers)} worktrees", file=sys.stderr)

        # First pass: gather worktree state in parallel
        records: list[dict[str, Any]] = []
        print("Gathering worktree state...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=16) as ex:
            futures = {ex.submit(audit_worktree, p, root): p for p in pointers}
            for done, f in enumerate(as_completed(futures), start=1):
                try:
                    records.append(f.result())
                except Exception as e:
                    records.append(
                        {
                            "path": str(futures[f].parent),
                            "errors": [f"audit failed: {e}"],
                        }
                    )
                if done % 50 == 0:
                    print(f"  {done}/{len(pointers)}", file=sys.stderr)

        # Build PR index for all repos seen
        repo_slugs = {r["repo_slug"] for r in records if r.get("repo_slug")}
        print(f"Building PR index for {len(repo_slugs)} repos...", file=sys.stderr)
        pr_idx = gather_pr_index(repo_slugs)

        # Second pass: classify
        for r in records:
            r["classification"] = classify(r, pr_idx)

        # Aggregate
        by_class: dict[str, list[dict[str, Any]]] = {}
        for r in records:
            by_class.setdefault(r["classification"], []).append(r)

        # Sort lists deterministically
        for items in by_class.values():
            items.sort(key=lambda r: r["path"])

        # Write JSON ledger
        ledger_json.parent.mkdir(parents=True, exist_ok=True)
        ledger_json.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "total": len(records),
                    "by_class_counts": {k: len(v) for k, v in sorted(by_class.items())},
                    "records": sorted(records, key=lambda r: r["path"]),
                },
                indent=2,
            )
        )

        # Markdown report
        md = []
        md.append("# Legacy Worktree Ledger\n")
        md.append(f"Root: `{root}`\n")
        md.append(f"Total worktrees: **{len(records)}**\n")
        md.append("\n## Counts by classification\n")
        md.append("| Classification | Count | Action |")
        md.append("|---|---:|---|")
        action_map = {
            "SAFE_REMOVE_MERGED": "delete (PR merged, clean tree)",
            "SAFE_REMOVE_GONE": "delete (no unique commits vs main, no remote branch)",
            "PUSH_THEN_REMOVE": "push unpushed commits, then delete",
            "PUSH_NEW_BRANCH": "push to a new salvage branch, link/create PR, then delete",
            "COMMIT_AND_REVIEW": "commit dirty changes to salvage branch, then PUSH_NEW_BRANCH",
            "DETACHED_REVIEW": "manual review (detached HEAD)",
            "ACTIVE_PR_LEAVE": "leave alone (open PR — work is live elsewhere)",
            "UNCERTAIN": "manual review",
        }
        for cls in sorted(by_class):
            md.append(
                f"| `{cls}` | {len(by_class[cls])} | {action_map.get(cls, '?')} |"
            )
        md.append("")

        # Per-class detail
        priority = [
            "COMMIT_AND_REVIEW",
            "PUSH_THEN_REMOVE",
            "PUSH_NEW_BRANCH",
            "DETACHED_REVIEW",
            "UNCERTAIN",
            "ACTIVE_PR_LEAVE",
            "SAFE_REMOVE_MERGED",
            "SAFE_REMOVE_GONE",
        ]
        for cls in priority:
            items = by_class.get(cls, [])
            if not items:
                continue
            md.append(f"\n## {cls} ({len(items)})\n")
            md.append(f"_Action: {action_map.get(cls, '?')}_\n")
            # Compact table
            md.append("| Path | Branch | Dirty | Ahead | Unique vs main | PR | Notes |")
            md.append("|---|---|---:|---:|---:|---|---|")
            for r in items[:200]:  # cap per section to keep doc readable
                pr_str = (
                    f"#{r['pr_number']} {r['pr_state']}" if r.get("pr_number") else "—"
                )
                short_path = r["path"].replace(str(root) + "/", "")
                note_bits = []
                if r["errors"]:
                    note_bits.append("err: " + "; ".join(r["errors"]))
                if r["upstream"] is None and not r["detached"]:
                    note_bits.append("no upstream")
                if r["remote_branch_exists"] is False:
                    note_bits.append("remote branch gone")
                notes = " / ".join(note_bits)
                md.append(
                    f"| `{short_path}` | `{r['branch'] or '?'}` | {r['dirty_files']} "
                    f"| {r['ahead']} | {r['unique_vs_main']} | {pr_str} | {notes} |"
                )
            if len(items) > 200:
                md.append(
                    f"\n_... {len(items) - 200} more entries (see JSON ledger)_\n"
                )

        ledger_md.write_text("\n".join(md))
        print(f"Wrote {ledger_json}", file=sys.stderr)
        print(f"Wrote {ledger_md}", file=sys.stderr)
        return ModelLegacyWorktreeAuditResult(
            ledger_json=ledger_json,
            ledger_md=ledger_md,
            total=len(records),
            by_class_counts={k: len(v) for k, v in sorted(by_class.items())},
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Legacy worktree audit")
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Directory containing worktree subdirectories",
    )
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="Directory to write ledger files"
    )
    args = parser.parse_args(argv)

    try:
        result = HandlerLegacyWorktreeAudit().handle(
            ModelLegacyWorktreeAuditRequest(root=args.root, out_dir=args.out_dir)
        )
    except LegacyWorktreeAuditError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\nSummary:", file=sys.stderr)
    for cls, count in result.by_class_counts.items():
        print(f"  {cls:24s} {count:4d}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
