# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A ``gh`` stand-in for contract items that read PR facts (OMN-20768).

Many ticket contracts carry evidence items that are themselves ``gh`` reads:
``gh pr view <n> --repo <r> --json files`` and
``gh api repos/<r>/contents/<path>?ref=<sha> --jq .content | base64 -d``. A lane
may not read GitHub (the lane gh guard refuses every read), so each such item
failed with the guard's refusal and the closeout sweep flipped nothing.

``EvidenceCollector`` puts this shim first on the PATH of a command check that
runs ``gh``. The shim answers the two read shapes above from the canonical clone
and the PR watcher's state (``DodEvidenceLocalSource``) and hands every other
invocation, and every field the local sources cannot hold, to the real ``gh``,
so a fact no local source holds keeps its old answer.

Supported locally:

* ``gh pr view <n> [--repo <r>] --json <fields> [--jq <expr>]`` for the fields
  ``number state mergedAt mergeCommit headRefName baseRefName headRefOid files
  title url isDraft additions deletions changedFiles``;
* ``gh api repos/<r>/contents/<path>[?ref=<ref>] [--jq <expr>]`` for a file in the
  clone, answered in the contents API's shape (base64 ``content``), or as the
  file's bytes when the request asks for the raw media type; no ``ref`` is the
  repository's landing branch;
* ``gh api repos/<r>/pulls/<n>/files [--paginate] [--jq <expr>]`` for a merged PR
  whose squash commit the clone holds;
* ``gh api [--cache <ttl>] repos/<r>/tarball/<sha>`` for a commit the clone holds.

A write (``gh pr merge``) is never in these shapes and always reaches the real
``gh``.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    DodEvidenceLocalSource,
)

REAL_GH_ENV: Final[str] = "ONEX_DOD_VERIFY_REAL_GH"
# ``python -m`` would warn on stderr (the package imports this module first), and
# a check's stderr is part of its failure text.
_ENTRY: Final[str] = (
    "import sys; "
    "from omnimarket.nodes.node_dod_verify.services.local_gh_reads import main; "
    "sys.exit(main())"
)
_JQ_TIMEOUT_S: Final[int] = 30

_PR_VIEW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "number",
        "state",
        "mergedAt",
        "mergeCommit",
        "headRefName",
        "baseRefName",
        "headRefOid",
        "files",
        "title",
        "url",
        "isDraft",
        "additions",
        "deletions",
        "changedFiles",
    }
)


def _flag(args: list[str], names: tuple[str, ...]) -> tuple[str | None, list[str]]:
    """``(value, remaining)`` for a ``--flag value`` / ``--flag=value`` option."""
    rest: list[str] = []
    value: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        if name in names:
            if eq:
                value = inline
            elif i + 1 < len(args):
                value = args[i + 1]
                i += 1
            else:
                return None, [*rest, arg]
        else:
            rest.append(arg)
        i += 1
    return value, rest


def _apply_jq(document: str, expression: str | None) -> str | None:
    if expression is None:
        return document + "\n"
    try:
        proc = subprocess.run(
            ["jq", "-r", expression],
            input=document,
            capture_output=True,
            text=True,
            timeout=_JQ_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _pr_view(args: list[str], source: DodEvidenceLocalSource) -> str | None:
    fields_arg, args = _flag(args, ("--json",))
    jq, args = _flag(args, ("--jq", "-q"))
    repo, args = _flag(args, ("--repo", "-R"))
    if fields_arg is None or len(args) != 1 or not args[0].isdigit():
        return None
    number = int(args[0])
    fields = [f for f in fields_arg.split(",") if f]
    if not fields or not set(fields) <= _PR_VIEW_FIELDS:
        return None
    repo = repo or source.cwd_repo()
    if not repo or repo.count("/") != 1:
        return None

    view = source.pr_view(repo, number)
    if view is None:
        return None
    merge_commit = view.get("mergeCommit")
    merge_sha = str(merge_commit["oid"]) if isinstance(merge_commit, dict) else ""
    is_merged = str(view.get("state")) == "MERGED"
    facts = source.commit_facts(repo, merge_sha) if merge_sha else None
    stats: list[dict[str, object]] | None = None
    if {"files", "additions", "deletions", "changedFiles"} & set(fields):
        parents = source.commit_parents(repo, merge_sha) if merge_sha else None
        stats = source.file_stats(repo, parents[0], merge_sha) if parents else None

    out: dict[str, Any] = {}
    for name in fields:
        if name == "number":
            out[name] = number
        elif name == "url":
            out[name] = f"https://github.com/{repo}/pull/{number}"
        elif name == "state":
            out[name] = view.get("state")
        elif name == "isDraft":
            if not is_merged:
                return None
            out[name] = False
        elif name == "mergedAt":
            merged_at = view.get("mergedAt") or (facts or {}).get("committed_at")
            if is_merged and not merged_at:
                return None
            out[name] = merged_at or None
        elif name == "mergeCommit":
            out[name] = {"oid": merge_sha} if merge_sha else None
        elif name in ("headRefName", "baseRefName", "headRefOid"):
            if not view.get(name):
                return None
            out[name] = view[name]
        elif name == "title":
            title = str((facts or {}).get("subject") or "")
            suffix = f" (#{number})"
            if not title.endswith(suffix):
                return None
            out[name] = title[: -len(suffix)]
        else:
            if stats is None:
                return None
            if name == "files":
                out[name] = stats
            elif name == "changedFiles":
                out[name] = len(stats)
            else:
                key = "additions" if name == "additions" else "deletions"
                out[name] = sum(int(str(row[key])) for row in stats)
    return _apply_jq(json.dumps(out, separators=(",", ":")), jq)


_RAW_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"application/vnd.github.raw", "application/vnd.github.v3.raw"}
)


def _api_args(args: list[str]) -> tuple[str, str | None, bool] | None:
    """``(endpoint, jq, raw)`` for a ``gh api`` GET with only the options the
    local reads honour; ``None`` for anything else (a write, a body, a header
    that changes the answer)."""
    endpoint: str | None = None
    jq: str | None = None
    raw = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        if name in ("--jq", "-q", "-H", "--header"):
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else None)
            if value is None:
                return None
            i += 1 if eq else 2
            if name in ("--jq", "-q"):
                jq = value
            else:
                header, _, media = value.partition(":")
                if header.strip().lower() != "accept":
                    return None
                if media.strip().lower() not in _RAW_MEDIA_TYPES:
                    return None
                raw = True
        elif arg == "--paginate":
            i += 1
        elif name == "--cache":
            i += 1 if eq else 2
        elif arg.startswith("-") or endpoint is not None:
            return None
        else:
            endpoint = arg
            i += 1
    return (endpoint, jq, raw) if endpoint else None


def _contents(
    repo: str,
    path: str,
    ref: str,
    jq: str | None,
    raw: bool,
    source: DodEvidenceLocalSource,
) -> str | None:
    data = source.file_at(repo, ref, path)
    if data is None:
        return None
    if raw:
        return data.decode("utf-8", errors="surrogateescape") if jq is None else None
    document = json.dumps(
        {
            "type": "file",
            "encoding": "base64",
            "path": path,
            "size": len(data),
            "content": base64.encodebytes(data).decode("ascii"),
        },
        separators=(",", ":"),
    )
    return _apply_jq(document, jq)


def _pull_files(
    repo: str, number: int, jq: str | None, source: DodEvidenceLocalSource
) -> str | None:
    view = source.pr_view(repo, number)
    commit = view.get("mergeCommit") if view else None
    merge_sha = str(commit["oid"]) if isinstance(commit, dict) else ""
    parents = source.commit_parents(repo, merge_sha) if merge_sha else None
    if not parents:
        return None
    named = source.changed_files(repo, parents[0], merge_sha)
    stats = source.file_stats(repo, parents[0], merge_sha)
    if named is None or stats is None:
        return None
    counts = {str(row["path"]): row for row in stats}
    rows: list[dict[str, object]] = []
    for item in named:
        row = counts.get(str(item["filename"]))
        if row is None:
            return None
        added, deleted = int(str(row["additions"])), int(str(row["deletions"]))
        rows.append(
            {
                "filename": item["filename"],
                "status": item["status"],
                "additions": added,
                "deletions": deleted,
                "changes": added + deleted,
            }
        )
    return _apply_jq(json.dumps(rows, separators=(",", ":")), jq)


def _api(args: list[str], source: DodEvidenceLocalSource) -> str | bytes | None:
    parsed = _api_args(args)
    if parsed is None:
        return None
    endpoint, jq, raw = parsed
    split = urlsplit(endpoint)
    parts = split.path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "repos":
        return None
    repo = f"{parts[1]}/{parts[2]}"
    if parts[3] == "tarball" and len(parts) == 5 and jq is None and not raw:
        return source.archive_at(repo, parts[4])
    if parts[3] == "contents" and len(parts) >= 5:
        refs = parse_qs(split.query).get("ref", [])
        if len(refs) > 1:
            return None
        ref = refs[0] if refs else source.landing_ref(repo)
        if not ref:
            return None
        return _contents(repo, "/".join(parts[4:]), ref, jq, raw, source)
    if (
        parts[3] == "pulls"
        and len(parts) == 6
        and parts[4].isdigit()
        and parts[5] == "files"
        and not raw
    ):
        return _pull_files(repo, int(parts[4]), jq, source)
    return None


def answer(
    argv: list[str], source: DodEvidenceLocalSource | None = None
) -> str | bytes | None:
    """The output of the ``gh`` invocation ``argv`` from local sources, or
    ``None`` when it is not a supported read or a local source lacks a fact."""
    local = source if source is not None else DodEvidenceLocalSource()
    if argv[:2] == ["pr", "view"]:
        return _pr_view(argv[2:], local)
    if argv[:1] == ["api"]:
        return _api(argv[1:], local)
    return None


def install_gh_shim(directory: Path) -> Path:
    """Write the ``gh`` shim into ``directory``; return the script's path."""
    script = directory / "gh"
    script.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_ENTRY)} "
        '"$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    text = answer(args)
    if text is not None:
        sys.stdout.buffer.write(
            text
            if isinstance(text, bytes)
            else text.encode("utf-8", errors="surrogateescape")
        )
        sys.stdout.buffer.flush()
        return 0
    real = os.environ.get(REAL_GH_ENV)
    if real:
        os.execv(real, [real, *args])
    sys.stderr.write("gh: command not found\n")
    return 127


__all__ = ["REAL_GH_ENV", "answer", "install_gh_shim", "main"]
