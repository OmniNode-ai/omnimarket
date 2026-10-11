# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Local read source for dod_verify's PR and commit facts (OMN-20838).

The operator ruling (2026-09-25, re-stated 2026-10-07): reads come from the
canonical clones (kept fresh every three minutes by canonical-clone-sync) and
the PR watcher's state file; ``gh`` is for writes. Before this module every
``onex skill dod_verify`` run read each bound PR from GitHub, three or four
times over, with paginated check-run listings, on whatever identity ``gh``
resolved to: eleven such spawns cost 64 calls of the operator's core bucket in
91 seconds on 2026-10-09.

This source answers, without GitHub:

* a PR's state, head, base and merge time, from the PR watcher's state
  (``$ONEX_PR_WATCHER_STATE``, else
  ``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``);
* a merged PR's squash commit, its parents and the files it changed, from the
  repository's canonical clone (``$OMNI_HOME/<repo>``): the squash commit is the
  one commit on the base branch whose subject ends ``(#<n>)``;
* the merged PRs whose title or branch names a ticket, from both;
* the newest copy of every check-run name at a PR's exact head, from the
  watcher's last read of that head.

Every method returns ``None`` when the local sources do not hold the fact, and
the caller then reads GitHub as before, so a fact no local source holds keeps
its old answer (the verdict-parity requirement). A fact held locally is never
also read from GitHub.

Freshness: a MERGED record is terminal and is served at any watcher age. Any
other state is served only while the state's last complete tick is at most
``WATCHER_MAX_AGE`` old, the same eight-minute rule ``pr_state_local.py``
applies; an older non-terminal record is not served.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

logger = logging.getLogger(__name__)

WATCHER_STATE_ENV: Final[str] = "ONEX_PR_WATCHER_STATE"
WATCHER_SCHEMA: Final[int] = 1
WATCHER_MAX_AGE: Final[timedelta] = timedelta(minutes=8)
_GIT_TIMEOUT_S: Final[int] = 60

# ``git diff --name-status`` letters to the GitHub pull-request files API's
# ``status`` values. ``T`` (type change) is GitHub's ``changed``.
_GIT_STATUS_TO_GITHUB: Final[dict[str, str]] = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "T": "changed",
}

# The parsed watcher file, keyed by (path, mtime): one parse per process.
_STATE_CACHE: dict[tuple[str, float], dict[str, Any]] = {}

_ORIGIN_URL_RE = re.compile(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def watcher_state_path() -> Path | None:
    """``$ONEX_PR_WATCHER_STATE``, else the watcher's file under ``$OMNI_HOME``."""
    declared = os.environ.get(WATCHER_STATE_ENV)
    if declared:
        return Path(declared)
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        return None
    return Path(registry_root) / ".claude_scratch" / "pr-watcher" / "state.json"


def canonical_clone(repo: str) -> Path | None:
    """The canonical clone under ``$OMNI_HOME`` for ``owner/name``, when one exists."""
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root or "/" not in repo:
        return None
    name = repo.split("/", 1)[1]
    candidate = Path(registry_root) / name
    if (candidate / ".git").exists():
        return candidate
    return None


def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    """Run one read-only git command; ``(rc, stdout)``. Never raises."""
    env = scrub_git_location_env(os.environ)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git %s in %s failed: %s", args[:2], repo_dir, exc)
        return 1, ""
    return proc.returncode, proc.stdout


def _parse_stamp(text: object) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class DodEvidenceLocalSource:
    """PR and commit facts from the PR watcher's state and the canonical clones.

    One instance per handler; the watcher file (about 15 MB) is parsed at most
    once per path and modification time per process.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now
        self.local_reads = 0

    # ------------------------------------------------------------ watcher

    def _state(self) -> dict[str, Any] | None:
        path = watcher_state_path()
        if path is None:
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = (str(path), mtime)
        cached = _STATE_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("PR watcher state %s unreadable: %s", path, exc)
            return None
        if not isinstance(raw, dict) or raw.get("schema") != WATCHER_SCHEMA:
            logger.warning("PR watcher state %s is not schema %d", path, WATCHER_SCHEMA)
            return None
        _STATE_CACHE.clear()
        _STATE_CACHE[key] = raw
        return raw

    def _state_is_fresh(self, state: dict[str, Any]) -> bool:
        last = _parse_stamp(state.get("last_tick"))
        if last is None:
            return False
        now = self._now or datetime.now(UTC)
        return now - last <= WATCHER_MAX_AGE

    def _short(self, repo: str) -> str:
        return repo.split("/", 1)[1] if "/" in repo else repo

    def _record(self, repo: str, number: int) -> dict[str, Any] | None:
        """The watcher's facts for ``repo#number`` as a ``gh pr view`` shape.

        Keys: ``state``, ``mergedAt``, ``headRefName``, ``baseRefName``,
        ``headRefOid``, plus ``mergeCommit`` when the watcher's merge record
        names the squash commit. ``None`` when not held, or held non-terminal
        and stale.
        """
        state = self._state()
        if state is None:
            return None
        key = f"{self._short(repo)}#{number}"
        prs = state.get("prs")
        rec = prs.get(key) if isinstance(prs, dict) else None
        merges = state.get("merges")
        merge = merges.get(key) if isinstance(merges, dict) else None
        if isinstance(rec, dict) and isinstance(rec.get("facts"), dict):
            facts = rec["facts"]
            out: dict[str, Any] = {
                "state": str(facts.get("state") or ""),
                "mergedAt": facts.get("merged_at") or None,
                "headRefName": str(facts.get("head_ref") or ""),
                "baseRefName": str(facts.get("base") or ""),
                "headRefOid": str(facts.get("head_sha") or ""),
            }
        elif isinstance(merge, dict):
            out = {
                "state": "MERGED",
                "mergedAt": merge.get("merged_at") or None,
                "headRefName": str(merge.get("head_ref") or ""),
                "baseRefName": str(merge.get("base") or ""),
                "headRefOid": str(merge.get("head_sha") or ""),
            }
        else:
            return None
        if not out["state"]:
            return None
        if out["state"] != "MERGED" and not self._state_is_fresh(state):
            return None
        if isinstance(merge, dict) and merge.get("merge_sha"):
            out["mergeCommit"] = {"oid": str(merge["merge_sha"])}
        return out

    # ------------------------------------------------------------ clone

    @staticmethod
    def _shallow(clone: Path) -> bool:
        rc, out = _git(clone, "rev-parse", "--is-shallow-repository")
        return rc != 0 or out.strip() != "false"

    def _clone_ref(self, clone: Path, branch: str) -> str | None:
        ref = f"refs/remotes/origin/{branch}"
        rc, _ = _git(clone, "rev-parse", "--verify", "--quiet", ref)
        return ref if rc == 0 else None

    def _default_branch(self, repo: str) -> str:
        state = self._state()
        repos = state.get("repos") if isinstance(state, dict) else None
        meta = repos.get(self._short(repo)) if isinstance(repos, dict) else None
        if isinstance(meta, dict) and meta.get("default_branch"):
            return str(meta["default_branch"])
        clone = canonical_clone(repo)
        if clone is not None:
            rc, out = _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            if rc == 0 and out.strip().startswith("origin/"):
                return out.strip()[len("origin/") :]
        return ""

    def squash_commit(self, repo: str, number: int, base: str = "") -> str | None:
        """The one commit on ``origin/<base>`` whose subject ends ``(#<number>)``."""
        clone = canonical_clone(repo)
        if clone is None:
            return None
        branch = base or self._default_branch(repo)
        ref = self._clone_ref(clone, branch) if branch else None
        if ref is None:
            return None
        rc, out = _git(
            clone,
            "log",
            "--first-parent",
            "--fixed-strings",
            f"--grep=(#{number})",
            "--format=%H%x09%s",
            ref,
        )
        if rc != 0:
            return None
        suffix = f"(#{number})"
        shas = [
            line.split("\t", 1)[0]
            for line in out.splitlines()
            if "\t" in line and line.split("\t", 1)[1].rstrip().endswith(suffix)
        ]
        return shas[0] if len(shas) == 1 else None

    def pr_view(self, repo: str, number: int) -> dict[str, Any] | None:
        """``gh pr view --json state,mergedAt,mergeCommit,headRefName,baseRefName,
        headRefOid`` from local sources, or ``None``.

        A record the watcher holds answers everything it knows; a merged one
        whose squash commit the watcher did not record takes it from the
        clone. A PR the watcher does not hold is answered from the clone only
        as MERGED with its squash commit and no head fields.
        """
        rec = self._record(repo, number)
        if rec is not None:
            if rec["state"] == "MERGED" and "mergeCommit" not in rec:
                sha = self.squash_commit(repo, number, rec["baseRefName"])
                if sha:
                    rec["mergeCommit"] = {"oid": sha}
            self.local_reads += 1
            return rec
        sha = self.squash_commit(repo, number)
        if sha is None:
            return None
        self.local_reads += 1
        return {
            "state": "MERGED",
            "mergedAt": None,
            "headRefName": "",
            "baseRefName": "",
            "headRefOid": "",
            "mergeCommit": {"oid": sha},
        }

    def commit_parents(self, repo: str, sha: str) -> list[str] | None:
        """``commits/<sha>``'s parents from the clone, or ``None``."""
        clone = canonical_clone(repo)
        if clone is None or not sha:
            return None
        rc, out = _git(clone, "rev-list", "--parents", "-n", "1", sha)
        parts = out.split()
        # No parent listed means a shallow boundary (a PR's squash commit is
        # never a root commit): the clone does not hold the answer.
        if rc != 0 or len(parts) < 2 or parts[0] != sha:
            return None
        self.local_reads += 1
        return parts[1:]

    def changed_files(
        self, repo: str, parent_sha: str, merge_sha: str
    ) -> list[dict[str, object]] | None:
        """The squash commit's changed files in the pull-request files shape."""
        clone = canonical_clone(repo)
        if clone is None or not parent_sha or not merge_sha:
            return None
        rc, out = _git(
            clone, "diff", "--name-status", "-M", "--no-color", parent_sha, merge_sha
        )
        if rc != 0:
            return None
        files: list[dict[str, object]] = []
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 2 or not cols[0]:
                continue
            status = _GIT_STATUS_TO_GITHUB.get(cols[0][0])
            if status is None:
                return None
            files.append({"filename": cols[-1], "status": status})
        self.local_reads += 1
        return files

    # ------------------------------------------------------------ tickets

    def merged_pr_candidates(
        self, repo: str, ticket_id: str, ticket_pattern: re.Pattern[str]
    ) -> list[dict[str, Any]]:
        """Merged PRs of ``repo`` whose title or branch matches the ticket token.

        The union of the watcher's records (open, recently merged and its merge
        records) and the squash commits on the clone's default branch. Each item
        is shaped like a ``gh pr list --json number,title,headRefName,
        headRepository`` row.
        """
        found: dict[int, dict[str, Any]] = {}
        short = self._short(repo)
        state = self._state()
        if state is not None:
            rows: list[dict[str, Any]] = []
            prs = state.get("prs")
            if isinstance(prs, dict):
                rows.extend(
                    r["facts"]
                    for r in prs.values()
                    if isinstance(r, dict)
                    and isinstance(r.get("facts"), dict)
                    and r["facts"].get("state") == "MERGED"
                )
            merges = state.get("merges")
            if isinstance(merges, dict):
                rows.extend(m for m in merges.values() if isinstance(m, dict))
            for row in rows:
                if row.get("repo") != short or not isinstance(row.get("number"), int):
                    continue
                title = str(row.get("title") or "")
                head_ref = str(row.get("head_ref") or "")
                if ticket_pattern.search(title) or ticket_pattern.search(head_ref):
                    found[row["number"]] = {
                        "number": row["number"],
                        "title": title,
                        "headRefName": head_ref,
                        "headRepository": {"nameWithOwner": repo},
                    }
        clone = canonical_clone(repo)
        branch = self._default_branch(repo)
        ref = self._clone_ref(clone, branch) if clone is not None and branch else None
        # A shallow clone (a hosted runner's checkout) holds part of the
        # history, so a search of it could miss a second candidate.
        if clone is not None and ref is not None and not self._shallow(clone):
            rc, out = _git(
                clone,
                "log",
                "--first-parent",
                "--regexp-ignore-case",
                "--fixed-strings",
                f"--grep={ticket_id}",
                "--format=%s",
                ref,
            )
            if rc == 0:
                for subject in out.splitlines():
                    match = re.search(r"\(#(\d+)\)\s*$", subject)
                    if match is None:
                        continue
                    title = subject[: match.start()].rstrip()
                    number = int(match.group(1))
                    if number in found or not ticket_pattern.search(title):
                        continue
                    found[number] = {
                        "number": number,
                        "title": title,
                        "headRefName": "",
                        "headRepository": {"nameWithOwner": repo},
                    }
        if found:
            self.local_reads += 1
        return [found[n] for n in sorted(found, reverse=True)]

    def cwd_repo(self, cwd: Path | None = None) -> str | None:
        """``owner/name`` of the working directory's ``origin`` remote."""
        rc, out = _git(cwd or Path.cwd(), "config", "--get", "remote.origin.url")
        match = _ORIGIN_URL_RE.search(out.strip()) if rc == 0 else None
        if match is None:
            return None
        return f"{match.group(1)}/{match.group(2)}"

    def pr_head_facts(self, repo: str, number: int) -> dict[str, Any] | None:
        """A merged or held PR's head sha, author, labels and title (OMN-20917).

        From the watcher's PR record, else its merge record; ``None`` when
        neither names a head sha, or a non-terminal record is stale.
        """
        state = self._state()
        if state is None:
            return None
        key = f"{self._short(repo)}#{number}"
        prs = state.get("prs")
        rec = prs.get(key) if isinstance(prs, dict) else None
        merges = state.get("merges")
        merge = merges.get(key) if isinstance(merges, dict) else None
        facts = rec.get("facts") if isinstance(rec, dict) else None
        source: dict[str, Any] | None = None
        if isinstance(facts, dict) and facts.get("head_sha"):
            if facts.get("state") != "MERGED" and not self._state_is_fresh(state):
                return None
            source = facts
        elif isinstance(merge, dict) and merge.get("head_sha"):
            source = merge
        if source is None:
            return None
        labels = source.get("labels")
        self.local_reads += 1
        return {
            "head_sha": str(source["head_sha"]),
            "author": str(source.get("author") or ""),
            "labels": [str(label) for label in labels]
            if isinstance(labels, list)
            else [],
            "title": str(source.get("title") or ""),
        }

    def head_check_run(
        self, repo: str, number: int, head_sha: str, name: str
    ) -> tuple[str, dict[str, Any] | None]:
        """The watcher's newest copy of check ``name`` at ``head_sha`` (OMN-20917).

        ``("found", {"conclusion", "id"})`` when the watcher's read of exactly
        that head holds a completed copy; ``("absent", None)`` when that read
        settled CI (a GREEN or RED verdict) with no copy of the name;
        ``("unknown", None)`` otherwise.
        """
        state = self._state()
        prs = state.get("prs") if isinstance(state, dict) else None
        rec = (
            prs.get(f"{self._short(repo)}#{number}") if isinstance(prs, dict) else None
        )
        ci = rec.get("ci") if isinstance(rec, dict) else None
        if not isinstance(ci, dict) or ci.get("sha") != head_sha:
            return "unknown", None
        ids = {
            str(row[0]): row[1]
            for row in ci.get("detail") or ()
            if isinstance(row, list) and len(row) >= 2
        }
        for row in ci.get("runs") or ():
            if not isinstance(row, list) or len(row) < 3 or str(row[0]) != name:
                continue
            if str(row[1]).lower() != "completed":
                return "unknown", None
            raw_id = str(ids.get(name) or "")
            self.local_reads += 1
            return "found", {
                "conclusion": str(row[2]).lower() if row[2] is not None else None,
                "id": int(raw_id) if raw_id.isdigit() else -1,
            }
        if ci.get("verdict") in {"GREEN", "RED"}:
            self.local_reads += 1
            return "absent", None
        return "unknown", None

    # ------------------------------------------------------------ checks

    def head_check_runs(
        self, repo: str, number: int, head_sha: str, required: list[str]
    ) -> tuple[list[dict[str, object]] | None, str]:
        """The watcher's last read of ``head_sha`` as check-run rows, when it
        settles every required context; else ``(None, why)``.

        Served only when all of these hold, so the rollup over these rows is
        the rollup the GitHub listing would give:

        * the watcher's record for ``repo#number`` was read at exactly
          ``head_sha``;
        * every required context has a completed newest copy there (so the
          merge commit's runs cannot change the outcome: a context with a
          completed head copy is decided by that copy, OMN-16055);
        * no other PR of the repository has the same head, since the state
          holds no check-suite branch attribution (OMN-15709).
        """
        state = self._state()
        if state is None:
            return None, "no PR watcher state"
        short = self._short(repo)
        prs = state.get("prs")
        if not isinstance(prs, dict):
            return None, "PR watcher state holds no PR records"
        rec = prs.get(f"{short}#{number}")
        if not isinstance(rec, dict):
            return None, f"{short}#{number} is not in the PR watcher state"
        raw_facts = rec.get("facts")
        facts: dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
        if facts.get("state") != "MERGED" and not self._state_is_fresh(state):
            return None, "PR watcher state is stale for a non-merged PR"
        ci = rec.get("ci")
        if not isinstance(ci, dict) or ci.get("sha") != head_sha:
            return None, f"the watcher holds no check-run read at {head_sha[:12]}"
        runs = ci.get("runs")
        if not isinstance(runs, list):
            return None, "the watcher's read carries no per-name rows"
        for other_key, other in prs.items():
            if other_key == f"{short}#{number}" or not isinstance(other, dict):
                continue
            ofacts = other.get("facts")
            if (
                isinstance(ofacts, dict)
                and ofacts.get("repo") == short
                and ofacts.get("head_sha") == head_sha
            ):
                return None, f"{other_key} shares head {head_sha[:12]}"
        ids = {
            str(row[0]): row[1]
            for row in ci.get("detail") or ()
            if isinstance(row, list) and len(row) >= 2
        }
        out: list[dict[str, object]] = []
        for row in runs:
            if not isinstance(row, list) or len(row) < 3:
                return None, "the watcher's read has a malformed row"
            name = str(row[0])
            raw_id = ids.get(name)
            out.append(
                {
                    "name": name,
                    "status": str(row[1]),
                    "conclusion": str(row[2]) if row[2] is not None else None,
                    "completed_at": str(row[3]) if len(row) > 3 else "",
                    "id": int(str(raw_id)) if str(raw_id or "").isdigit() else -1,
                }
            )
        by_name = {str(r["name"]): r for r in out}
        unsettled = [
            n
            for n in required
            if n not in by_name or str(by_name[n]["status"]).lower() != "completed"
        ]
        if unsettled:
            return None, (
                f"{len(unsettled)} required context(s) absent or not completed in "
                f"the watcher's read of {head_sha[:12]}"
            )
        self.local_reads += 1
        return out, f"pr-watcher read_at={ci.get('read_at')}"


__all__ = [
    "WATCHER_MAX_AGE",
    "WATCHER_STATE_ENV",
    "DodEvidenceLocalSource",
    "canonical_clone",
    "watcher_state_path",
]
