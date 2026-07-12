# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every git-SHA pin in ``uv.lock`` must be reachable from a remote branch (OMN-14450).

The failure this exists to prevent (OMN-14447)
----------------------------------------------
``omnimarket/uv.lock`` pinned ``omnibase-core`` at ``af567d7c`` -- the HEAD of the
*feature branch* of ``omnibase_core#1432``. GitHub **deletes a feature branch when
its PR merges**, so that commit became reachable from nothing on the remote. The
OMN-12977 sibling-pin preflight then aborted the workspace build permanently: no
clone state can satisfy a pin to an object no clone can fetch. It blocked the
OMN-14437 effects-runtime rebuild outright.

**Pin the squash-merged commit on the default branch, never a feature-branch head.**
Squash-merged commits are reachable forever; branch heads evaporate the moment the
PR lands.

Why this check only means something in CI
-----------------------------------------
``git branch -r --contains <sha>`` **LIES on any un-pruned clone.** A clone that
fetched the branch *before* deletion keeps the stale remote-tracking ref and
reports the object as reachable. The first platform sweep for OMN-14447 returned
"0 unreachable everywhere" -- and it was wrong, by exactly the mechanism that
created the bug.

So this check **prunes before it looks**. On a developer's stale clone the naive
form is green on a lock that is already broken for CI and for every fresh clone;
that is the whole trap, and pruning is the whole fix.

Exit codes: 0 = every pin reachable | 1 = at least one unreachable.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# `source = { git = "https://github.com/OmniNode-ai/<repo>.git?rev=<40-hex>#<40-hex>" }`
_GIT_PIN_RE = re.compile(
    r"git\s*=\s*\"https://github\.com/(?P<org>[\w-]+)/(?P<repo>[\w-]+)\.git\?rev=(?P<rev>[0-9a-f]{40})"
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def extract_pins(lock_text: str) -> list[tuple[str, str]]:
    """Return the unique ``(repo, rev)`` git-SHA pins declared in a uv.lock."""
    return sorted({(m["repo"], m["rev"]) for m in _GIT_PIN_RE.finditer(lock_text)})


def is_reachable(repo_dir: Path, rev: str, *, prune: bool = True) -> bool:
    """True when ``rev`` is reachable from a remote branch of the clone at ``repo_dir``.

    Prunes first. Without the prune this returns True for a commit whose branch was
    deleted upstream -- the exact false-negative that let OMN-14447 through.
    """
    if prune:
        _git(["fetch", "origin", "--prune", "--quiet"], cwd=repo_dir)

    # The object may not even be present on a fresh clone -- that alone is a failure.
    if _git(["cat-file", "-e", f"{rev}^{{commit}}"], cwd=repo_dir).returncode != 0:
        return False

    branches = _git(["branch", "-r", "--contains", rev], cwd=repo_dir)
    return branches.returncode == 0 and bool(branches.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--clones-root",
        type=Path,
        required=True,
        help="Directory holding sibling clones, one per pinned repo (e.g. $OMNI_HOME).",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip the prune. ONLY for proving that the naive check gives a false PASS.",
    )
    args = parser.parse_args(argv)

    pins = extract_pins(args.lock.read_text(encoding="utf-8"))
    if not pins:
        print(f"no git-SHA pins in {args.lock} -- nothing to check")
        return 0

    unreachable: list[tuple[str, str]] = []
    for repo, rev in pins:
        repo_dir = args.clones_root / repo
        if not (repo_dir / ".git").exists():
            print(f"  SKIP        {repo} @ {rev[:9]} (no clone at {repo_dir})")
            continue
        if is_reachable(repo_dir, rev, prune=not args.no_prune):
            print(f"  OK          {repo} @ {rev[:9]}")
        else:
            print(f"  UNREACHABLE {repo} @ {rev[:9]}")
            unreachable.append((repo, rev))

    if not unreachable:
        print(f"\nuv.lock pin reachability OK: {len(pins)} git pin(s) checked")
        return 0

    print(
        f"\n{len(unreachable)} git-SHA pin(s) in {args.lock} are NOT reachable from any "
        f"remote branch:\n"
    )
    for repo, rev in unreachable:
        print(f"  {repo} @ {rev}")
    print(
        "\nA pin to an unreachable commit makes the workspace build IMPOSSIBLE for every\n"
        "fresh clone (CI, the .201 build host): the OMN-12977 sibling-pin preflight can\n"
        "never be satisfied, because no clone can fetch the object.\n"
        "\n"
        "This almost always means the rev is a FEATURE-BRANCH HEAD whose branch GitHub\n"
        "deleted when its PR merged (OMN-14447).\n"
        "\n"
        "  Fix: pin the SQUASH-MERGED commit on the default branch, never a branch head.\n"
        "       Squash-merged commits are reachable forever; branch heads evaporate the\n"
        "       moment the PR lands.\n"
        "\n"
        "  Note: `git branch -r --contains` LIES on an un-pruned clone -- it reports a\n"
        "        deleted branch's commit as reachable. Always `git fetch --prune` first.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
