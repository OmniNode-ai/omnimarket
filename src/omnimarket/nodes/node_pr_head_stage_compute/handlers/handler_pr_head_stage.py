# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the shell that puts a remote lane's ``--pr`` heads where its engine reads them (OMN-17427).

The fetch step runs before the worktree exists: it uses the host clone's commit, or fetches the head into the host's bare cache
when the host clone lacks it. The worktree is then made in the host clone whenever that clone holds the base sha, so a head left in
the cache never reached the lane's own clone (the omnimarket#4137 Codex lane on host-b, 2026-10-18: ``fatal: bad object`` on the
watcher's head, and a lane-side refresh that could not run from a worktree whose ``.git`` link was set aside). The stage step runs
after the worktree is made and copies each head into the clone that owns it, over local git transport from whichever of the cache
and the host clone holds it: no network read, no ref and no ``FETCH_HEAD`` written in the shared clone.
"""

from __future__ import annotations

import shlex

from ..models.model_pr_head_stage import ModelPrHeadStageRequest, ModelPrHeadStageResult


def fetch_script(request: ModelPrHeadStageRequest) -> str:
    """Use the watcher's commit from the host clone, or refresh the bare cache when it is absent."""
    owner, registry_root = request.owner, request.registry_root
    # A portable Git on PATH can otherwise run an older system Git for its built-in
    # subprocesses. Keep those on the selected binary and retain external helpers.
    lines = [
        request.path_export,
        "GIT_HELPERS=$(git --exec-path) || exit 70",
        "GIT_BIN=$(command -v git) || exit 70",
        'export GIT_EXEC_PATH="${GIT_BIN%/*}" PATH="$PATH:$GIT_HELPERS"',
        'LR="$HOME/lab-run"',
        'mkdir -p "$LR/repos" || exit 70',
    ]
    host_clone = registry_root is not None and owner == "OmniNode-ai"
    for head in request.heads:
        repo, ref, sha = head.repo, head.head_ref, head.head_sha
        destination = f"refs/remotes/origin/{ref}"
        lines.append(f'R="$LR/repos/{repo}.git"')
        if host_clone:
            lines += [
                f"K={shlex.quote(f'{registry_root}/{repo}')}",
                f'if [ -e "$K/.git" ] && git -C "$K" cat-file -e {sha}^{{commit}}; then',
                f'  printf "REMOTE-LANE-PR-HEAD %s %s\\n" {shlex.quote(head.pr)} {sha}',
                "else",
            ]
        lines += [
            '[ -d "$R" ] || git init --bare -q "$R" || exit 70',
            "GIT_TERMINAL_PROMPT=0 git "
            f'-C "$R" fetch --no-tags -q {shlex.quote(f"https://github.com/{owner}/{repo}.git")} '
            f"{shlex.quote(f'+refs/heads/{ref}:{destination}')} || exit 20",
            f'git -C "$R" cat-file -e {sha}^{{commit}} || exit 21',
            f'F=$(git -C "$R" rev-parse --verify {shlex.quote(destination)}) || exit 21',
            f'printf "REMOTE-LANE-PR-HEAD %s %s\\n" {shlex.quote(head.pr)} "$F"',
        ]
        if host_clone:
            lines.append("fi")
    return "\n".join(lines)


def stage_script(request: ModelPrHeadStageRequest) -> str:
    """Copy each of ``request.repo``'s PR heads into the clone that owns the lane's worktree, read from ``$C/repository``.

    Prints ``REMOTE-LANE-PR-HEAD-STAGED <pr> <sha> <clone>`` per head; exits 21 when neither the cache nor the host clone holds a
    head, 22 when the local copy fails. Another repository's heads are not in this worktree's clone and are skipped. Empty when the
    request names no repository or run."""
    repo, run_id = request.repo, request.run_id
    if not repo or not run_id:
        return ""
    cache = f'"$LR/repos/{repo}.git"'
    lines = [
        f'LR="$HOME/lab-run"; C="$LR/runs/{run_id}.ctx"; R={cache}',
        'if [ -f "$C/repository" ]; then IFS= read -r R < "$C/repository" || exit 70; fi',
        "K="
        + (
            shlex.quote(f"{request.registry_root}/{repo}")
            if request.registry_root is not None
            else '""'
        ),
    ]
    for head in request.heads:
        if head.repo != repo:
            continue
        pr, sha = shlex.quote(head.pr), head.head_sha
        lines += [
            f'if ! git -C "$R" cat-file -e {sha}^{{commit}} 2>/dev/null; then',
            f'  S=""; for X in {cache} "$K"; do',
            f'    if [ -n "$X" ] && [ "$X" != "$R" ] && git -C "$X" cat-file -e {sha}^{{commit}} 2>/dev/null; then S=$X; break; fi',
            "  done",
            f'  [ -n "$S" ] || {{ printf "REMOTE-LANE-PR-HEAD-STAGE-MISSING %s %s\\n" {pr} {sha} >&2; exit 21; }}',
            f'  git -C "$R" fetch --no-tags --no-write-fetch-head -q "$S" {sha} || exit 22',
            "fi",
            f'git -C "$R" cat-file -e {sha}^{{commit}} || exit 21',
            f'printf "REMOTE-LANE-PR-HEAD-STAGED %s %s %s\\n" {pr} {sha} "$R"',
        ]
    return "\n".join(lines)


class HandlerPrHeadStage:
    def handle(self, request: ModelPrHeadStageRequest) -> ModelPrHeadStageResult:
        return ModelPrHeadStageResult(fetch_script(request), stage_script(request))
