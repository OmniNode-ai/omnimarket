# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the registry-clone half of the lab host parity probe and judge what it reads (OMN-17427).

``CLONE_PARITY_PY`` runs inside the placement module's parity probe, on the host under its own python3 (3.6 or newer, stdlib
only), after the probe has defined ``root``, ``clones``, ``git`` and ``clean``. It prints ``PARITY-CLONE <name> <present|missing> <head>
<tip> <hook 1|0>`` per clone and ``PARITY-CORE-WORKTREE <clone> <directory>`` for a clone whose ``core.worktree`` names a directory
other than the clone itself. On 2026-10-18 a lane run set that in a canonical clone to its own run directory: ``git status`` read
clean over a stale tree, a convergence reset the run directory, and the plugin tick crashed for every session.
"""

from __future__ import annotations

from ..models.model_clone_parity import ModelCloneParityRequest, ModelCloneParityResult

CLONE_PARITY_PY = r"""def stray_worktree(path):
    # core.worktree in the clone's own config naming any directory but the clone itself (2026-10-18: a lane run set it to its run
    # directory in a canonical clone, and every git command on the clone read the wrong tree). None: unset, or the clone itself.
    if not path or not os.path.exists(os.path.join(path, ".git")):
        return None
    value = git(path, "config", "--local", "--get", "core.worktree")
    if not value:
        return None
    where = value if os.path.isabs(value) else os.path.join(path, ".git", value)
    return None if os.path.realpath(where) == os.path.realpath(path) else where


def is_clone(path):
    # A clone whose core.worktree names another directory has a different toplevel, and it is still the clone: it is read as
    # present and fails on its core.worktree, not as a missing clone to provision over.
    top = git(path, "rev-parse", "--show-toplevel") if path and os.path.isdir(path) else None
    return (top is not None and os.path.realpath(top) == os.path.realpath(path)) or stray_worktree(path) is not None


def clone_path(name):
    # Private repositories sit beside the registry root, not under it (2026-10-17: several hosts refused
    # the private data clone), so a clone is read under the root first, else beside it.
    if not root:
        return ""
    under = os.path.join(root, name)
    beside = os.path.join(root, "..", name)
    return beside if not is_clone(under) and is_clone(beside) else under


print("PARITY-HOME " + (root or "none"))
for name in [c for c in clones.split(",") if c]:
    path = clone_path(name)
    if not is_clone(path):
        print("PARITY-CLONE %s missing - - -" % name)
        continue
    head = git(path, "rev-parse", "HEAD") or "-"
    stray = stray_worktree(path)
    if stray:
        print("PARITY-CORE-WORKTREE %s %s" % (name, clean(stray)))
    tip = None
    origin_head = git(path, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    for ref in ([origin_head] if origin_head else []) + ["refs/remotes/origin/main", "refs/remotes/origin/dev"]:
        tip = git(path, "rev-parse", "--verify", "-q", ref + "^{commit}")
        if tip:
            break
    hooks = git(path, "config", "--get", "core.hooksPath")
    if hooks:
        hooks = os.path.expanduser(hooks)
    else:
        hooks = git(path, "rev-parse", "--git-path", "hooks") or os.path.join(".git", "hooks")
    hook = os.path.join(path, hooks, "pre-commit")
    print("PARITY-CLONE %s present %s %s %d" % (name, head, tip or "-", 1 if os.access(hook, os.X_OK) and os.path.isfile(hook) else 0))


def git_identity():
    # Whether git, outside any repository, knows who the lane account is (system and global config): on 2026-10-20 one lab host passed
    # every hook, then git refused the commit with "Author identity unknown".
    return tuple("1" if git("/", "config", "--get", "user." + key) else "0" for key in ("name", "email"))


print("PARITY-GITIDENT %s %s" % git_identity())
"""


# The repair half (OMN-19998): the remote-lane runner's provision script runs these per clone, after it has set ``n`` (the clone's
# name), ``D`` (its directory, empty when it is missing), ``NEW`` (1 when it was cloned just now) and ``fail``. A local
# change never blocks a fast-forward by itself: git refuses one that would overwrite a change (a modified tracked file or an
# untracked file an incoming commit adds) and keeps every other, so the dirty paths are named in the PROVISIONED line or the
# refusal (2026-10-18: the canonical private knowledge-base clones on two lab hosts were 323 commits behind with 1629 dirty
# paths, none of them touched by the incoming commits, and every kb lane ended no-host on "the clone has uncommitted changes").
# The default branch is re-read from the remote first (OMN-20266): refs/remotes/origin/HEAD is cut at clone time and fetch never moves it, so a
# clone kept on dev by the host reconciler was refused as "not the default branch main" after the remote default became dev.
PROVISION_FAST_FORWARD_SH = r"""if [ -n "$D" ] && [ "$NEW" = 0 ]; then
  git -C "$D" fetch -q origin >/dev/null 2>&1 || true
  case "$(git -C "$D" config --get remote.origin.url)" in git@github.com:*|https://github.com/*|ssh://git@github.com/*)
    ONEX_CANONICAL_CONVERGE=1 git -C "$D" remote set-head origin --auto >/dev/null 2>&1 || true;; esac
  ref=$(git -C "$D" symbolic-ref -q refs/remotes/origin/HEAD) || ref=""
  if [ -z "$ref" ]; then for b in main dev; do git -C "$D" rev-parse -q --verify "refs/remotes/origin/$b^{commit}" >/dev/null && { ref="refs/remotes/origin/$b"; break; }; done; fi
  def="${ref#refs/remotes/origin/}"; cur=$(git -C "$D" symbolic-ref -q --short HEAD) || cur=""
  if [ -z "$ref" ]; then fail "clone:$n" fast-forward "no origin tip to fast-forward to"
  elif [ "$cur" != "$def" ]; then fail "clone:$n" fast-forward "checked out ${cur:-a detached HEAD}, not the default branch $def"
  else
    dirty=$(git -C "$D" status --porcelain 2>&1 | cut -c4-); nd=$(printf "%s" "$dirty" | grep -c .)
    names=$(printf "%s\n" "$dirty" | head -n 10 | tr "\n" " " | sed "s/ *$//"); [ "$nd" -gt 10 ] && names="$names ..."
    if out=$(git -C "$D" merge --ff-only -q "$ref" 2>&1); then kept=""
      [ "$nd" -gt 0 ] && kept=" (kept $nd local change(s): $names)"
      printf "PROVISIONED clone:%s fast-forward %s%s\n" "$n" "$(git -C "$D" rev-parse --short=12 HEAD)" "$kept"
    elif [ "$nd" -gt 0 ]; then fail "clone:$n" fast-forward "the clone has $nd uncommitted path(s): $names; git: $out"
    else fail "clone:$n" fast-forward "$out"; fi
  fi
fi"""
# pre-commit refuses to install while core.hooksPath is set, so an override holding no hook is named, not installed over.
PROVISION_HOOK_SH = r"""if [ -n "$D" ]; then
  hp=$(git -C "$D" config --get core.hooksPath) || hp=""
  case "$hp" in "~"*) hp="$HOME${hp#\~}";; esac
  [ -n "$hp" ] || hp=$(git -C "$D" rev-parse --git-path hooks)
  case "$hp" in /*) H="$hp/pre-commit";; *) H="$D/$hp/pre-commit";; esac
  if [ -f "$H" ] && [ -x "$H" ]; then :
  elif [ ! -f "$D/.pre-commit-config.yaml" ]; then fail "hook:$n" hook "the clone has no .pre-commit-config.yaml to install a hook from"
  elif [ -n "$(git -C "$D" config --get core.hooksPath)" ]; then
    fail "hook:$n" hook "core.hooksPath is $hp, which holds no executable pre-commit, and pre-commit install refuses while it is set"
  elif ! command -v pre-commit >/dev/null 2>&1; then fail "hook:$n" hook "pre-commit is not on the PATH"
  else out=$(cd "$D" && pre-commit install -t pre-commit -t commit-msg 2>&1); prc=$?
    if [ "$prc" = 0 ] && [ -f "$H" ] && [ -x "$H" ]; then printf "PROVISIONED hook:%s hook %s\n" "$n" "$H"
    else fail "hook:$n" hook "pre-commit install ($(command -v pre-commit)) exited $prc and left no executable hook at $H${out:+: $out}"; fi
  fi
fi"""


def core_worktree_failure(name: str, path: str) -> tuple[str, str]:
    """The parity failure ``(item, reason)`` for clone ``name`` whose core.worktree names ``path``."""
    return (
        f"core-worktree:{name}",
        f"clone {name} has core.worktree set to {path}, not to itself, so every git command on it "
        "reads that directory's tree (git -C <clone> config --unset core.worktree)",
    )


def parse_git_identity(text: str) -> tuple[bool, bool] | None:
    """The probe's last ``PARITY-GITIDENT`` line as (user.name set, user.email set); None from an older probe."""
    for line in reversed(text.splitlines()):
        parts = line.split()
        if len(parts) == 3 and parts[0] == "PARITY-GITIDENT":
            return parts[1] == "1", parts[2] == "1"
    return None


def git_identity_failure(name: bool, email: bool) -> tuple[str, str] | None:
    """The ``git-identity`` parity failure ``(item, reason)`` for a unit that commits, or None when git knows the account."""
    unset = " and ".join(
        key for key, ok in (("user.name", name), ("user.email", email)) if not ok
    )
    return (
        (
            "git-identity",
            f"{unset} unset for the lane account, so a commit fails with `Author identity unknown` after every "
            "hook passed; `onex-host-install lane-account` sets the lab commit identity",
        )
        if unset
        else None
    )


class HandlerCloneParity:
    def handle(self, request: ModelCloneParityRequest) -> ModelCloneParityResult:
        item, reason = core_worktree_failure(request.name, request.path)
        return ModelCloneParityResult(
            clone_parity_py=CLONE_PARITY_PY, failure_item=item, failure_reason=reason
        )
