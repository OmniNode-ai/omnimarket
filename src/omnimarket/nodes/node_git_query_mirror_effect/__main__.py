# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_git_query_mirror_effect -- the clone query layer (OMN-19617).

Callers (the merge drain, provers, reviewers, landers) run this instead of
``gh`` for reads git can answer. Each call prints one JSON object per result
that names its ``source`` (mirror, mirror_refreshed, gh_fallback, unavailable)
and the mirror's ``watermark_age_s``.

Usage::

    python -m omnimarket.nodes.node_git_query_mirror_effect sync            # every registry repo
    python -m omnimarket.nodes.node_git_query_mirror_effect sync --repo omnimarket
    python -m omnimarket.nodes.node_git_query_mirror_effect head-sha --repo omnimarket --pr 2905
    python -m omnimarket.nodes.node_git_query_mirror_effect changed-files --repo omnimarket --pr 2905
    python -m omnimarket.nodes.node_git_query_mirror_effect diff --repo omnimarket --pr 2905 --text
    python -m omnimarket.nodes.node_git_query_mirror_effect conflicts --repo omnimarket --pr 2905
    python -m omnimarket.nodes.node_git_query_mirror_effect on-base --repo omnimarket --pr 2905
    python -m omnimarket.nodes.node_git_query_mirror_effect log --repo omnimarket --pr 2905
    python -m omnimarket.nodes.node_git_query_mirror_effect status --repo omnimarket

The mirror root is ``$ONEX_GIT_QUERY_MIRROR_ROOT``, else
``$ONEX_STATE_DIR/git-query-mirrors``; with neither set the CLI refuses.
Exit status: 0 when every result is ok, 1 otherwise, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_git_query_mirror_effect.git_mirror import (
    REGISTRY_REPOS,
    GitQueryMirror,
    resolve_mirror_root,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    DEFAULT_MAX_AGE_S,
    EnumGitQueryOperation,
    ModelGitQueryRequest,
    ModelGitQueryResult,
)

_DEFAULT_OWNER = "OmniNode-ai"
_COMMANDS: dict[str, EnumGitQueryOperation] = {
    "sync": EnumGitQueryOperation.SYNC,
    "status": EnumGitQueryOperation.STATUS,
    "head-sha": EnumGitQueryOperation.HEAD_SHA,
    "changed-files": EnumGitQueryOperation.CHANGED_FILES,
    "diff": EnumGitQueryOperation.DIFF,
    "conflicts": EnumGitQueryOperation.CONFLICTS,
    "on-base": EnumGitQueryOperation.ON_BASE,
    "log": EnumGitQueryOperation.LOG,
}


def _slug(repo: str) -> str:
    return repo if "/" in repo else f"{_DEFAULT_OWNER}/{repo}"


def _emit(result: ModelGitQueryResult, text: bool) -> None:
    if text and result.ok and result.diff is not None:
        sys.stdout.write(result.diff)
        return
    if text and result.ok and result.files is not None:
        sys.stdout.write("".join(f"{f}\n" for f in result.files))
        return
    sys.stdout.write(result.model_dump_json(exclude_none=True) + "\n")


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    parser.print_usage(sys.stderr)
    sys.stderr.write(f"{parser.prog}: error: {message}\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_git_query_mirror_effect",
        description="Answer PR reads from a fetch-only git mirror instead of the GitHub API.",
    )
    parser.add_argument("command", choices=sorted(_COMMANDS))
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="owner/name or bare name (owner defaults to OmniNode-ai); repeat for sync/status.",
    )
    parser.add_argument("--pr", type=int, default=None, help="PR number.")
    parser.add_argument(
        "--base", default=None, help="Base branch (default: the repo default branch)."
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_AGE_S,
        help="Seconds; an older mirror refreshes the PR ref by git fetch first (0 = always).",
    )
    parser.add_argument(
        "--no-gh-fallback",
        action="store_true",
        help="Never call the GitHub API; report unavailable instead.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Print the raw diff (diff) or one path per line (changed-files) instead of JSON.",
    )
    args = parser.parse_args(argv)
    op = _COMMANDS[args.command]

    try:
        root = resolve_mirror_root()
    except KeyError:
        return _usage_error(parser, "set ONEX_GIT_QUERY_MIRROR_ROOT or ONEX_STATE_DIR")
    mirror = GitQueryMirror(root=root)

    repos = [_slug(r) for r in (args.repo or [])]
    if op in (EnumGitQueryOperation.SYNC, EnumGitQueryOperation.STATUS):
        targets = repos or list(REGISTRY_REPOS)
    else:
        if len(repos) != 1 or args.pr is None:
            return _usage_error(
                parser, f"{args.command} needs exactly one --repo and --pr"
            )
        targets = repos

    all_ok = True
    for repo in targets:
        request = ModelGitQueryRequest(
            operation=op,
            repo=repo,
            pr_number=args.pr,
            base=args.base,
            max_age_s=args.max_age,
            allow_gh_fallback=not args.no_gh_fallback,
        )
        result = mirror.query(request)
        all_ok = all_ok and result.ok
        _emit(result, args.text)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
