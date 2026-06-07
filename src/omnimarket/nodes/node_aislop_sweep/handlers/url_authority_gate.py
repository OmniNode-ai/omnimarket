# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""url-authority ratchet gate CLI (OMN-12818, PR-2 of OMN-12803).

Enforcement entry point for the pre-commit hook and the required CI check. The
gate FAILS only on NEW violations (fingerprints absent from the frozen baseline);
existing violations are grandfathered. The baseline is burn-down only.

Usage::

    # default: scan explicitly-passed files (staged), fail on any NEW violation
    python -m omnimarket.nodes.node_aislop_sweep.handlers.url_authority_gate FILE...

    # full-repo scan (CI) — scan every tracked .py under the repo
    python -m ...url_authority_gate --all --repo-root . --repo omnimarket

    # regenerate the baseline (burn-down only — may shrink, never grow)
    python -m ...url_authority_gate --update-baseline --repo-root . --repo omnimarket

Exit codes: 0 clean / grandfathered-only · 1 NEW violation(s) or baseline grew.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnimarket.nodes.node_aislop_sweep.handlers.url_authority import (
    UrlAuthorityViolation,
    assert_baseline_shrinks_only,
    load_baseline,
    partition_against_baseline,
    scan_source,
    scan_tree,
    serialize_baseline,
)

# Baseline lives next to the check, beside the node's other static assets.
_BASELINE_PATH = (
    Path(__file__).parent.parent / "baselines" / "url_authority_baseline.json"
)


def _scan_paths(
    repo: str, repo_root: Path, paths: list[str]
) -> list[UrlAuthorityViolation]:
    """Scan an explicit list of files (the staged set for the pre-commit path)."""
    violations: list[UrlAuthorityViolation] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_file() or p.suffix != ".py":
            continue
        try:
            source = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(p.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            rel = str(p)
        violations.extend(scan_source(repo, rel, source))
    return violations


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


def _out(message: str) -> None:
    sys.stdout.write(message + "\n")


def _print_new(new: list[UrlAuthorityViolation]) -> None:
    _err(
        f"URL-AUTHORITY GATE FAILED: {len(new)} NEW violation(s) — every URL must "
        "resolve from a contract (routing authority / integration catalog), not a "
        "literal or a *_URL/*_ENDPOINT env read.\n"
    )
    for v in new:
        _err(f"  [{v.rule}] {v.repo}/{v.path}:{v.line}")
        _err(f"    {v.snippet}")
        _err("    -> migrate to the resolver, or annotate # url-authority-ok: <reason>")


def _baseline_entries(baseline_path: Path) -> list[dict[str, str]]:
    if not baseline_path.exists():
        return []
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    entries = data.get("violations", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict) and "fingerprint" in e]


def _update_baseline(
    repo: str, repo_root: Path, baseline_path: Path, *, seed: bool
) -> int:
    """Regenerate THIS repo's baseline subset from a full scan.

    Entries for other repos in the single cross-repo baseline document are
    preserved untouched. In ``seed`` mode the per-repo subset is established with
    no shrink check (one-time initialization). Otherwise the subset may only
    shrink relative to the prior subset — you cannot grow the grandfathered set.
    """
    prior = _baseline_entries(baseline_path)
    repo_before = {e["fingerprint"] for e in prior if e.get("repo") == repo}
    other_entries = [e for e in prior if e.get("repo") != repo]

    fresh: list[dict[str, str]] = serialize_baseline(scan_tree(repo, repo_root))[
        "violations"
    ]  # type: ignore[assignment]
    repo_after = {e["fingerprint"] for e in fresh}

    if not seed:
        assert_baseline_shrinks_only(repo_before, repo_after)

    merged = sorted(
        [*other_entries, *fresh],
        key=lambda e: (e["repo"], e["path"], e["fingerprint"]),
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {"schema_version": "1.0.0", "count": len(merged), "violations": merged},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    action = "seeded" if seed else f"burned down {len(repo_before) - len(repo_after)}"
    _out(
        f"URL-AUTHORITY BASELINE updated for {repo}: {len(repo_after)} violation(s) "
        f"({action}). Total across repos: {len(merged)}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="url-authority ratchet gate (OMN-12818)."
    )
    parser.add_argument("paths", nargs="*", help="Explicit files to scan (staged set).")
    parser.add_argument(
        "--repo", default="omnimarket", help="Repo name for fingerprints."
    )
    parser.add_argument(
        "--repo-root", default=".", help="Repo root for repo-relative paths."
    )
    parser.add_argument(
        "--all", action="store_true", help="Full-repo scan instead of explicit files."
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Regenerate the baseline for this repo (burn-down only — may not grow).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="One-time initialization of this repo's baseline subset (no shrink check).",
    )
    parser.add_argument(
        "--baseline",
        default=str(_BASELINE_PATH),
        help="Path to the baseline JSON.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    baseline_path = Path(args.baseline)

    if args.update_baseline or args.seed:
        try:
            return _update_baseline(args.repo, repo_root, baseline_path, seed=args.seed)
        except ValueError as exc:
            _err(f"URL-AUTHORITY BASELINE REJECTED: {exc}")
            return 1

    if args.all:
        violations = scan_tree(args.repo, repo_root)
    else:
        if not args.paths:
            _out("URL-AUTHORITY GATE: no files to scan.")
            return 0
        violations = _scan_paths(args.repo, repo_root, args.paths)

    baseline = load_baseline(baseline_path)
    new, grandfathered = partition_against_baseline(violations, baseline)
    if new:
        _print_new(new)
        return 1
    _out(
        "URL-AUTHORITY GATE PASSED: 0 new violations "
        f"({len(grandfathered)} grandfathered)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
