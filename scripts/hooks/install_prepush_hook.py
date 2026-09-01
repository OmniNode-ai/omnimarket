#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the worktree-aware common pre-push bootstrap (OMN-17461).

Git shares ``$GIT_COMMON_DIR/hooks`` among linked worktrees.  The bootstrap
written by this module is deliberately stable: at *push* time it resolves the
active worktree with ``git rev-parse --show-toplevel`` and asks pre-commit to
run that worktree's checked-out configuration.  It must therefore never be a
symlink into a canonical clone's mutable working tree.
"""

from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

HOOK_NAME = "pre-push"
LEGACY_HOOK_RELATIVE_PATH = Path("scripts/hooks/prepush_smart_tests.sh")
BOOTSTRAP_MARKER = "OMN-17461 worktree-aware pre-push bootstrap"

BOOTSTRAP = f"""#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# {BOOTSTRAP_MARKER}
#
# This file lives in $GIT_COMMON_DIR/hooks, which all linked worktrees share.
# Resolve the *invoking* worktree before selecting policy; sourcing a canonical
# clone's hook here would apply a different commit's gate to this push.
set -euo pipefail

die() {{
  printf '[prepush-bootstrap] ERROR: %s\\n' "$1" >&2
  exit 1
}}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || die "not inside a git worktree"
config="${{repo_root}}/.pre-commit-config.yaml"
[ -f "$config" ] \
  || die "missing $config; refusing to run a different worktree's pre-push policy"
required_hook="${{repo_root}}/scripts/hooks/prepush_smart_tests.sh"
[ -x "$required_hook" ] \
  || die "missing executable $required_hook; refusing to silently downgrade the governed pre-push gate"
grep -Fq "id: prepush-smart-tests" "$config" \
  && grep -Fq "entry: bash scripts/hooks/prepush_smart_tests.sh" "$config" \
  && grep -Fq "stages: [pre-push]" "$config" \
  || die "missing governed pre-push hook authority in $config; refusing a vacuous pass"

common_dir="$(git rev-parse --git-common-dir 2>/dev/null)" \
  || die "could not resolve git common directory"
case "$common_dir" in
  /*) ;;
  *) common_dir="${{repo_root}}/${{common_dir}}" ;;
esac
hook_dir="${{common_dir}}/hooks"
[ -d "$hook_dir" ] \
  || die "missing common hook directory $hook_dir"

cd "$repo_root"
if command -v pre-commit >/dev/null 2>&1; then
  exec pre-commit hook-impl --config "$config" --hook-type pre-push --hook-dir "$hook_dir" -- "$@"
fi
for python in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$python" >/dev/null 2>&1 && "$python" -c 'import pre_commit' >/dev/null 2>&1; then
    exec "$python" -m pre_commit hook-impl --config "$config" --hook-type pre-push --hook-dir "$hook_dir" -- "$@"
  fi
done
die "pre-commit is unavailable; refusing to report a vacuous pre-push pass"
"""


class HookInstallError(RuntimeError):
    """Raised when replacing a common hook would be unsafe."""


def _git_output(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise HookInstallError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def common_hook_path(*, cwd: Path) -> Path:
    common_dir = Path(_git_output(["rev-parse", "--git-common-dir"], cwd=cwd))
    if not common_dir.is_absolute():
        common_dir = (cwd / common_dir).resolve()
    return common_dir / "hooks" / HOOK_NAME


def _main_worktree(*, cwd: Path) -> Path:
    output = _git_output(["worktree", "list", "--porcelain"], cwd=cwd)
    for line in output.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree ")).resolve()
    raise HookInstallError("git worktree list returned no primary worktree")


def _legacy_targets(*, repo_root: Path) -> set[Path]:
    """Known unsafe hook targets for this repository only.

    The common hook normally points at the primary checkout, not the linked
    worktree invoking this installer. Accept precisely those two repository
    locations; never turn an arbitrary external symlink into a replacement.
    """
    return {
        (repo_root / LEGACY_HOOK_RELATIVE_PATH).resolve(),
        (_main_worktree(cwd=repo_root) / LEGACY_HOOK_RELATIVE_PATH).resolve(),
    }


def _is_replaceable(hook_path: Path, *, repo_root: Path) -> bool:
    if not hook_path.exists() and not hook_path.is_symlink():
        return True
    if hook_path.is_symlink():
        try:
            return hook_path.resolve() in _legacy_targets(repo_root=repo_root)
        except OSError:
            return False
    try:
        current = hook_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return current == BOOTSTRAP or BOOTSTRAP_MARKER in current


def check_installation(*, cwd: Path) -> tuple[bool, str]:
    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
    hook_path = common_hook_path(cwd=repo_root)
    if hook_path.is_symlink():
        return (
            False,
            f"{hook_path} is a symlink; expected a regular worktree-aware bootstrap",
        )
    try:
        actual = hook_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read {hook_path}: {exc}"
    if actual != BOOTSTRAP:
        return False, f"{hook_path} is not the OMN-17461 worktree-aware bootstrap"
    if not hook_path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return False, f"{hook_path} is not executable"
    return True, f"{hook_path} is the OMN-17461 worktree-aware bootstrap"


def install(*, cwd: Path) -> Path:
    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
    hook_path = common_hook_path(cwd=repo_root)
    if not _is_replaceable(hook_path, repo_root=repo_root):
        raise HookInstallError(
            f"refusing to replace unknown common hook {hook_path}; inspect it before changing hook policy"
        )
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.is_symlink() or hook_path.exists():
        hook_path.unlink()
    hook_path.write_text(BOOTSTRAP, encoding="utf-8")
    hook_path.chmod(
        hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return hook_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="verify the common hook")
    actions.add_argument(
        "--install", action="store_true", help="install the safe bootstrap"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cwd = Path.cwd()
    try:
        if args.check:
            ok, message = check_installation(cwd=cwd)
            print(message)
            return 0 if ok else 1
        hook_path = install(cwd=cwd)
    except HookInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"installed worktree-aware pre-push bootstrap at {hook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
