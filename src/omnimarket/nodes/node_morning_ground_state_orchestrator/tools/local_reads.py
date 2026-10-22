# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only morning tools over local sources: no network call, no GitHub read.

PR, CI and branch state comes from the PR watcher's projection (the one process that polls
GitHub), merge history and workflow files from the canonical clones, documents from the
knowledge-base clone, and lane facts from the rolling ledger and its archive. Every place
is a deployment overlay location; the agent names a repository, an artifact key, a plan
file name or a heading, never a path.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Annotated, Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from .base import (
    LEDGER_ROW_RE,
    ArtifactKey,
    ModelNoInput,
    MorningTool,
    MorningToolRefusalError,
    Result,
    _git,
    _git_blob,
    _git_out,
)
from .context import ModelMorningToolContext

REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$"
RepoName = Annotated[str, StringConstraints(pattern=REPO_PATTERN)]
TICKET_PATTERN = r"^OMN-\d{1,6}$"
TicketId = Annotated[str, StringConstraints(pattern=TICKET_PATTERN)]
PLAN_FILE_PATTERN = r"^\d{4}-\d{2}-\d{2}-[A-Za-z0-9._-]{1,140}\.md$"
CHECK_NAME_PATTERN = r"^[^\n\r\x00]{1,160}$"
CHUNK_CHARS = 40000
TICKET_RE = re.compile(r"\bOMN-\d{1,6}\b")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _chunk(text: str, offset: int) -> Result:
    lines = text.splitlines()
    out: list[str] = []
    size = 0
    index = offset
    while index < len(lines) and size + len(lines[index]) + 1 <= CHUNK_CHARS:
        out.append(lines[index])
        size += len(lines[index]) + 1
        index += 1
    return {
        "line_count": len(lines),
        "from_line": offset,
        "next_offset": index if index < len(lines) else None,
        "text": "\n".join(out),
    }


class ModelOffsetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset_lines: int = Field(default=0, ge=0, le=100000)


class ModelArtifactReadInput(ModelOffsetInput):
    artifact: ArtifactKey


class ModelSourceDocInput(ModelOffsetInput):
    doc: str = Field(
        pattern=r"^[a-z][a-z0-9_]{1,40}$",
        description="a source document key the run declares (see kb_source_doc_keys)",
    )


class ModelPlanReadInput(ModelOffsetInput):
    plan: str = Field(
        pattern=PLAN_FILE_PATTERN,
        description="a dated plan file name under the knowledge base's plans directory",
    )


class ModelReadmeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    heading: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9 .,:#()'/&—-]{2,90}$",
        description="text of one heading to return with its section; omit for the heading list",
    )


class ModelRepoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: RepoName


class ModelWorkflowRunsInput(ModelRepoInput):
    workflow: str = Field(
        pattern=r"^[A-Za-z0-9._-]{1,80}\.ya?ml$",
        description="workflow file name, e.g. evidence-autoclose-sweep.yml",
    )
    limit: int = Field(default=10, ge=1, le=40)


class ModelWorkflowTriggersInput(ModelRepoInput):
    contexts: (
        list[Annotated[str, StringConstraints(pattern=CHECK_NAME_PATTERN)]] | None
    ) = Field(
        default=None,
        max_length=40,
        description="keep only workflows with a job whose display name is one of these",
    )


class ModelPullInput(ModelRepoInput):
    pr: int = Field(ge=1, le=10_000_000)


class ModelTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: TicketId


class ModelSinceHoursInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since_hours: int = Field(default=72, ge=1, le=336)


class ModelPlanPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=30, ge=1, le=120)
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=60, ge=1, le=120)


# --- the PR watcher projection -----------------------------------------------------------


@cache
def _watcher_cached(path: str, mtime_ns: int) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Path(path).read_text(encoding="utf-8")))


def _watcher(ctx: ModelMorningToolContext) -> dict[str, Any]:
    path = ctx.place("pr_watcher_state")
    try:
        stat = path.stat()
    except OSError as exc:
        raise MorningToolRefusalError(
            f"the PR watcher state is unreadable: {exc}"
        ) from None
    return _watcher_cached(str(path), stat.st_mtime_ns)


def _watcher_meta(state: dict[str, Any]) -> Result:
    tick = _parse(state.get("last_tick"))
    return {
        "source": "PR watcher projection (no GitHub read by this tool)",
        "last_tick": state.get("last_tick"),
        "last_full_resync": state.get("last_full_resync"),
        "age_seconds": int((_now() - tick).total_seconds()) if tick else None,
    }


def _integration_branch(state: dict[str, Any], repo: str) -> str:
    repos = cast(dict[str, Any], state.get("repos") or {})
    if repo not in repos:
        raise MorningToolRefusalError(
            f"{repo} is not a repository the PR watcher tracks; tracked: {sorted(repos)}"
        )
    return str(repos[repo].get("default_branch") or "main")


def _clone(ctx: ModelMorningToolContext, repo: str) -> Path:
    clone = ctx.place("clones_root") / repo
    if not (clone / ".git").exists():
        raise MorningToolRefusalError(f"no canonical clone of {repo} on this host")
    return clone


def watcher_branch_head(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The integration head, its required contexts and the check runs on it, attributed."""
    p = cast(ModelRepoInput, params)
    state = _watcher(ctx)
    branch = _integration_branch(state, p.repo)
    record = cast(
        dict[str, Any], (state.get("branches") or {}).get(f"{p.repo}@{branch}")
    )
    if not record:
        return {
            **_watcher_meta(state),
            "repo": p.repo,
            "branch": branch,
            "status": "UNKNOWN",
            "reason": f"the watcher holds no record for {p.repo}@{branch}",
        }
    settings = cast(dict[str, Any], (state.get("settings") or {}).get(p.repo) or {})
    protection = cast(
        dict[str, Any], (settings.get("branches") or {}).get(branch) or {}
    )
    sha = str(record.get("sha") or "")
    checks = cast(dict[str, Any], record.get("checks") or {})
    push = cast(dict[str, Any], record.get("push") or {})
    runs = cast(dict[str, Any], record.get("runs") or {})
    push_runs = {
        str(r[0]): {
            "workflow": r[1],
            "status": r[2],
            "conclusion": r[3],
            "head_sha": r[5],
        }
        for r in push.get("rows") or []
        if isinstance(r, list) and len(r) >= 6
    }
    branch_runs = {
        str(r[0]): {"path": r[2], "workflow": r[3], "event": r[5], "head_branch": r[8]}
        for r in runs.get("rows") or []
        if isinstance(r, list) and len(r) >= 10
    }
    rows: list[dict[str, Any]] = []
    for r in checks.get("rows") or []:
        if not isinstance(r, list) or len(r) < 8:
            continue
        run_id = str(r[7])
        if run_id in push_runs:
            event, head_branch = "push", branch
        elif run_id in branch_runs:
            event = branch_runs[run_id]["event"]
            head_branch = branch_runs[run_id]["head_branch"]
        else:
            event, head_branch = None, None
        rows.append(
            {
                "name": r[0],
                "status": r[1],
                "conclusion": r[2],
                "started_at": r[3],
                "run_id": run_id,
                "event": event,
                "head_branch": head_branch,
            }
        )
    required = [
        *cast(list[str], protection.get("contexts") or []),
        *cast(list[str], protection.get("rule_contexts") or []),
    ]
    verdicts: list[JsonValue] = []
    for name in dict.fromkeys(required):
        named = [x for x in rows if x["name"] == name]
        qualifying = [
            x for x in named if x["event"] == "push" and x["head_branch"] == branch
        ]
        if qualifying:
            newest = max(qualifying, key=lambda x: str(x["started_at"] or ""))
            if newest["status"] != "completed":
                verdict = "PENDING"
            elif newest["conclusion"] in ("failure", "cancelled", "timed_out"):
                verdict = "FAILING"
            else:
                verdict = "PASSING"
        else:
            verdict = "ABSENT"
        verdicts.append(
            {
                "context": name,
                "verdict": verdict,
                "qualifying_runs": len(qualifying),
                "other_runs": cast(
                    JsonValue,
                    [
                        {
                            k: x[k]
                            for k in ("status", "conclusion", "event", "head_branch")
                        }
                        for x in named
                        if x not in qualifying
                    ][:5],
                ),
            }
        )
    failing_push = sorted(
        {
            str(x["workflow"])
            for x in push_runs.values()
            if x["conclusion"] in ("failure", "cancelled", "timed_out")
        }
    )
    return {
        **_watcher_meta(state),
        "repo": p.repo,
        "branch": branch,
        "head_sha": sha,
        "tip_source": record.get("tip_source"),
        "tip_read_at": record.get("tip_read_at"),
        "checks_read_at": checks.get("read_at"),
        "checks_sha": checks.get("sha"),
        "checks_total": checks.get("total"),
        "checks_truncated": checks.get("truncated"),
        "unread": cast(JsonValue, record.get("unread") or {}),
        "protection": protection.get("protection") or "UNKNOWN (no protection read)",
        "required_mechanism": "required_status_checks contexts and ruleset contexts",
        "rulesets": cast(JsonValue, settings.get("rulesets") or []),
        "required": verdicts,
        "failing_push_workflows_at_head": cast(JsonValue, failing_push),
        "note": (
            "ABSENT means no push-event check run on this branch at this sha; read the "
            "workflow's own triggers (workflow_triggers) before calling a context that only "
            "runs on pull requests or the merge queue ABSENT: it is NOT APPLICABLE there."
        ),
    }


def watcher_release_branch(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The release branch (main) record of a release-synced repository, for the main-sync row."""
    p = cast(ModelRepoInput, params)
    state = _watcher(ctx)
    record = cast(
        dict[str, Any], (state.get("branches") or {}).get(f"{p.repo}@main") or {}
    )
    if not record or _integration_branch(state, p.repo) == "main":
        return {
            "repo": p.repo,
            "release_branch": None,
            "reason": "no separate release branch",
        }
    push = cast(dict[str, Any], record.get("push") or {})
    failing = [
        {"run_id": r[0], "workflow": r[1], "conclusion": r[3], "created_at": r[4]}
        for r in push.get("rows") or []
        if isinstance(r, list)
        and len(r) >= 6
        and r[3] in ("failure", "cancelled", "timed_out")
    ]
    return {
        **_watcher_meta(state),
        "repo": p.repo,
        "release_branch": "main",
        "head_sha": record.get("sha"),
        "push_runs_read_at": push.get("read_at"),
        "failing_push_runs": cast(JsonValue, failing[:20]),
    }


def watcher_workflow_runs(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The newest runs of one workflow the watcher holds for the repo's branches."""
    p = cast(ModelWorkflowRunsInput, params)
    state = _watcher(ctx)
    _integration_branch(state, p.repo)
    found: list[dict[str, Any]] = []
    windows: dict[str, JsonValue] = {}
    for key, record in cast(dict[str, Any], state.get("branches") or {}).items():
        if not key.startswith(f"{p.repo}@"):
            continue
        runs = cast(dict[str, Any], record.get("runs") or {})
        windows[key] = {
            "read_at": runs.get("read_at"),
            "rows": len(runs.get("rows") or []),
        }
        for r in runs.get("rows") or []:
            if (
                isinstance(r, list)
                and len(r) >= 10
                and str(r[2]).endswith("/" + p.workflow)
            ):
                found.append(
                    {
                        "run_id": r[0],
                        "workflow": r[3],
                        "attempt": r[4],
                        "event": r[5],
                        "status": r[6],
                        "conclusion": r[7],
                        "head_branch": r[8],
                        "created_at": r[9],
                    }
                )
    found.sort(key=lambda x: str(x["created_at"]), reverse=True)
    return {
        **_watcher_meta(state),
        "repo": p.repo,
        "workflow": p.workflow,
        "runs": cast(JsonValue, found[: p.limit]),
        "runs_found": len(found),
        "watcher_windows": windows,
        "note": (
            "Only the runs inside the watcher's recent-run window are visible here; zero "
            "runs found is UNKNOWN for older history, never proof that none ran. Job steps "
            "and logs are not in the projection."
        ),
    }


def merged_prs_in_window(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Pull requests merged into the integration branch since the run's window start."""
    p = cast(ModelRepoInput, params)
    state = _watcher(ctx)
    branch = _integration_branch(state, p.repo)
    since = ctx.window_start or ctx.date
    clone = _clone(ctx, p.repo)
    log = _git(
        clone,
        "log",
        f"refs/remotes/origin/{branch}",
        "--first-parent",
        f"--since={since}T00:00:00Z",
        "--format=%H%x1f%cI%x1f%s",
    )
    if log["exit"] != 0:
        raise MorningToolRefusalError(f"git log failed: {log['stderr']}")
    merges = cast(dict[str, Any], state.get("merges") or {})
    rows: list[JsonValue] = []
    for line in str(log["stdout"]).splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, when, subject = parts
        number = re.search(r"\(#(\d+)\)\s*$", subject)
        pr = int(number.group(1)) if number else None
        watched = merges.get(f"{p.repo}#{pr}") if pr else None
        rows.append(
            {
                "pr": pr,
                "title": subject[:160],
                "merge_sha": sha,
                "committed_at": when,
                "merged_at": (watched or {}).get("merged_at"),
                "head_ref": (watched or {}).get("head_ref"),
            }
        )
    return {
        "repo": p.repo,
        "branch": branch,
        "since": since,
        "source": f"canonical clone {p.repo} refs/remotes/origin/{branch} (first-parent)",
        "clone_synced_at": cast(
            JsonValue,
            ((state.get("branches") or {}).get(f"{p.repo}@{branch}") or {}).get(
                "clone_synced_at"
            ),
        ),
        "merged_in_window": len(rows),
        "rows": rows[:150],
        "truncated": len(rows) > 150,
    }


def pull_merge_state(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Whether one pull request merged, with its merge sha, from the watcher and the clone."""
    p = cast(ModelPullInput, params)
    state = _watcher(ctx)
    branch = _integration_branch(state, p.repo)
    key = f"{p.repo}#{p.pr}"
    merged = cast(dict[str, Any], (state.get("merges") or {}).get(key) or {})
    if merged:
        return {
            "pr": key,
            "state": "MERGED",
            "merged_at": merged.get("merged_at"),
            "merge_sha": merged.get("merge_sha"),
            "base": merged.get("base"),
            "title": merged.get("title"),
            "source": "PR watcher merges",
        }
    opened = cast(dict[str, Any], (state.get("prs") or {}).get(key) or {})
    if opened:
        facts = cast(dict[str, Any], opened.get("facts") or {})
        return {
            "pr": key,
            "state": "OPEN",
            "title": facts.get("title"),
            "head_sha": facts.get("head_sha") or facts.get("head"),
            "class": opened.get("cls"),
            "blocked": opened.get("blocked"),
            "queued": opened.get("queued"),
            "reason": opened.get("reason"),
            "source": "PR watcher open PRs",
        }
    clone = _clone(ctx, p.repo)
    found = _git_out(
        clone,
        "log",
        f"refs/remotes/origin/{branch}",
        "--first-parent",
        "-E",
        f"--grep=\\(#{p.pr}\\)$",
        "--format=%H%x1f%cI%x1f%s",
        "-1",
    )
    if found:
        sha, when, subject = found.split("\x1f", 2)
        return {
            "pr": key,
            "state": "MERGED",
            "merge_sha": sha,
            "committed_at": when,
            "title": subject,
            "source": f"canonical clone {p.repo} origin/{branch} history",
        }
    return {
        "pr": key,
        "state": "UNKNOWN",
        "reason": "not an open PR in the watcher, not in its merge record, and no squash "
        f"commit (#{p.pr}) on origin/{branch}: closed unmerged, or merged elsewhere",
    }


def ticket_prs(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Open and merged pull requests that carry a ticket id in their title or branch."""
    p = cast(ModelTicketInput, params)
    state = _watcher(ctx)
    needle = p.ticket.lower()
    pattern = re.compile(rf"{re.escape(needle)}(?!\d)")
    opened: list[JsonValue] = []
    for key, record in cast(dict[str, Any], state.get("prs") or {}).items():
        facts = cast(dict[str, Any], record.get("facts") or {})
        text = f"{facts.get('title', '')} {facts.get('head_ref', '')}".lower()
        if pattern.search(text):
            opened.append(
                {
                    "pr": key,
                    "title": str(facts.get("title", ""))[:160],
                    "class": record.get("cls"),
                    "blocked": record.get("blocked"),
                    "queued": record.get("queued"),
                }
            )
    merged: list[JsonValue] = []
    for key, record in cast(dict[str, Any], state.get("merges") or {}).items():
        text = f"{record.get('title', '')} {record.get('head_ref', '')}".lower()
        if pattern.search(text):
            merged.append(
                {
                    "pr": key,
                    "title": str(record.get("title", ""))[:160],
                    "merged_at": record.get("merged_at"),
                    "merge_sha": record.get("merge_sha"),
                }
            )
    history: list[JsonValue] = []
    for repo in ctx.integration_repos:
        clone = ctx.place("clones_root") / repo
        if not (clone / ".git").exists():
            continue
        try:
            branch = _integration_branch(state, repo)
        except MorningToolRefusalError:
            continue
        found = _git_out(
            clone,
            "log",
            f"refs/remotes/origin/{branch}",
            "--first-parent",
            "-i",
            f"--grep={p.ticket}",
            "-E",
            "--format=%H%x1f%cI%x1f%s",
            "-30",
        )
        for line in (found or "").splitlines():
            sha, when, subject = ([*line.split("\x1f", 2), "", ""])[:3]
            if not pattern.search(subject.lower()):
                continue
            number = re.search(r"\(#(\d+)\)\s*$", subject)
            history.append(
                {
                    "repo": repo,
                    "pr": f"{repo}#{number.group(1)}" if number else None,
                    "merge_sha": sha,
                    "committed_at": when,
                    "title": subject[:160],
                }
            )
    return {
        **_watcher_meta(state),
        "ticket": p.ticket,
        "open": opened[:40],
        "merged": merged[:60],
        "merged_in_history": history[:60],
        "note": "merged: the watcher's merge record; merged_in_history: squash commits on "
        "each integration repo's branch whose subject names the ticket",
    }


def workflow_triggers(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Each workflow's triggers and job names, read from the canonical clone's branch tip."""
    p = cast(ModelWorkflowTriggersInput, params)
    state = _watcher(ctx)
    branch = _integration_branch(state, p.repo)
    clone = _clone(ctx, p.repo)
    ref = f"refs/remotes/origin/{branch}"
    listing = _git_out(clone, "ls-tree", "--name-only", f"{ref}:.github/workflows")
    if listing is None:
        return {
            "repo": p.repo,
            "branch": branch,
            "workflows": [],
            "reason": "no workflows",
        }
    wanted = set(p.contexts or [])
    out: list[JsonValue] = []
    for name in listing.splitlines():
        if not name.endswith((".yml", ".yaml")):
            continue
        text = _git_blob(clone, f"{ref}:.github/workflows/{name}") or ""
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            out.append({"file": name, "error": str(exc)[:200]})
            continue
        if not isinstance(loaded, dict):
            continue
        triggers = loaded.get("on", loaded.get(True))
        if isinstance(triggers, (dict, list)):
            events = sorted(str(k) for k in triggers)
        else:
            events = [str(triggers)]
        jobs = loaded.get("jobs") if isinstance(loaded.get("jobs"), dict) else {}
        job_names = [
            str((job or {}).get("name") or job_id)
            if isinstance(job, dict)
            else str(job_id)
            for job_id, job in cast(dict[str, Any], jobs).items()
        ]
        if wanted and not any(
            j in wanted or any(j.startswith(w) or w.startswith(j) for w in wanted)
            for j in job_names
        ):
            continue
        out.append(
            {
                "file": name,
                "name": str(loaded.get("name") or name),
                "triggers": cast(JsonValue, events),
                "jobs": cast(JsonValue, job_names[:40]),
            }
        )
    return {"repo": p.repo, "branch": branch, "ref": ref, "workflows": out[:120]}


# --- documents --------------------------------------------------------------------------


def morning_readme(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The morning process README: its heading list, or one section by heading text."""
    p = cast(ModelReadmeInput, params)
    path = ctx.place("process_readme")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = [(i, line) for i, line in enumerate(lines) if line.startswith("#")]
    if p.heading is None:
        return {"headings": cast(JsonValue, [h for _, h in headings])}
    needle = p.heading.lower().lstrip("# ").strip()
    match = next(((i, h) for i, h in headings if needle in h.lower()), None)
    if match is None:
        raise MorningToolRefusalError(f"no heading contains {p.heading!r}")
    start, heading = match
    level = len(heading) - len(heading.lstrip("#"))
    end = next(
        (i for i, h in headings if i > start and len(h) - len(h.lstrip("#")) <= level),
        len(lines),
    )
    section = "\n".join(lines[start:end])
    return {
        "heading": heading,
        "lines": end - start,
        "truncated": len(section) > CHUNK_CHARS,
        "text": section[:CHUNK_CHARS],
    }


def kb_read_artifact(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """One morning artifact's text: the run's worktree copy, else origin/main's."""
    p = cast(ModelArtifactReadInput, params)
    relative = ctx.artifact_path(p.artifact)
    local = ctx.kb_worktree / relative
    branch = (
        _git_out(ctx.kb_worktree, "branch", "--show-current")
        if ctx.kb_worktree.is_dir()
        else None
    )
    if branch == ctx.kb_branch and local.is_file():
        text = local.read_text(encoding="utf-8", errors="replace")
        status = _git_out(ctx.kb_worktree, "status", "--porcelain", "--", relative)
        commit = _git_out(ctx.kb_worktree, "log", "-1", "--format=%H", "--", relative)
        return {
            "artifact": p.artifact,
            "path": relative,
            "copy": f"run worktree on {ctx.kb_branch}",
            "committed_sha": commit or "",
            "clean": status == "",
            **_chunk(text, p.offset_lines),
        }
    blob = _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{relative}")
    if blob is None:
        return {
            "artifact": p.artifact,
            "path": relative,
            "copy": None,
            "exists": False,
            "checked": [f"run worktree on {ctx.kb_branch}", "origin/main"],
        }
    return {
        "artifact": p.artifact,
        "path": relative,
        "copy": "origin/main of the knowledge-base clone",
        **_chunk(blob, p.offset_lines),
    }


def kb_source_doc_keys(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    return {"source_docs": cast(JsonValue, dict(ctx.source_docs))}


def kb_read_source_doc(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """SOURCE-DOC RESOLUTION: the knowledge-base copy by basename first, then the workspace."""
    p = cast(ModelSourceDocInput, params)
    if p.doc not in ctx.source_docs:
        raise MorningToolRefusalError(
            f"no source document {p.doc!r}; declared: {sorted(ctx.source_docs)}"
        )
    relative = ctx.source_docs[p.doc]
    basename = Path(relative).name
    checked: list[str] = []
    for folder in ("beta/plans", "beta/tracking"):
        spec = f"refs/remotes/origin/main:{folder}/{basename}"
        checked.append(f"knowledge base origin/main {folder}/{basename}")
        blob = _git_blob(ctx.kb_clone, spec)
        if blob is not None:
            return {
                "doc": p.doc,
                "declared_path": relative,
                "copy_read": f"knowledge base origin/main {folder}/{basename}",
                "checked": cast(JsonValue, checked),
                **_chunk(blob, p.offset_lines),
            }
    local = ctx.root / relative
    checked.append(f"workspace {relative}")
    if local.is_file():
        return {
            "doc": p.doc,
            "declared_path": relative,
            "copy_read": f"workspace {relative}",
            "checked": cast(JsonValue, checked),
            **_chunk(
                local.read_text(encoding="utf-8", errors="replace"), p.offset_lines
            ),
        }
    return {
        "doc": p.doc,
        "declared_path": relative,
        "copy_read": None,
        "verdict": "UNKNOWN",
        "checked": cast(JsonValue, checked),
    }


def _plans_dir(ctx: ModelMorningToolContext) -> str:
    return ctx.facts.get("PLANS_DIR", "beta/plans").rstrip("/")


def kb_read_plan(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelPlanReadInput, params)
    relative = f"{_plans_dir(ctx)}/{p.plan}"
    blob = _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{relative}")
    if blob is None:
        raise MorningToolRefusalError(f"no plan {relative} on origin/main")
    return {"plan": relative, **_chunk(blob, p.offset_lines)}


def _dated_plans(ctx: ModelMorningToolContext, days: int) -> list[tuple[str, str, int]]:
    folder = _plans_dir(ctx)
    listing = _git_out(
        ctx.kb_clone, "ls-tree", "--name-only", f"refs/remotes/origin/main:{folder}"
    )
    if listing is None:
        raise MorningToolRefusalError(f"cannot list {folder} on origin/main")
    day = datetime.strptime(ctx.date, "%Y-%m-%d").date()
    found: list[tuple[str, str, int]] = []
    for name in listing.splitlines():
        match = re.match(r"^(\d{4}-\d{2}-\d{2})-.*\.md$", name)
        if not match:
            continue
        try:
            minted = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (day - minted).days
        if 0 <= age <= days:
            found.append((name, match.group(1), age))
    return found


def _lifecycle(ctx: ModelMorningToolContext) -> dict[str, dict[str, Any]]:
    relative = ctx.facts.get(
        "PLAN_LIFECYCLE_PATH", f"{_plans_dir(ctx)}/PLAN_LIFECYCLE.yaml"
    )
    blob = _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{relative}")
    if blob is None:
        return {}
    loaded = yaml.safe_load(blob)
    entries: dict[str, dict[str, Any]] = {}
    items: Any = loaded.get("plans") if isinstance(loaded, dict) else loaded
    if isinstance(items, dict):
        items = [
            {"path": k, **(v if isinstance(v, dict) else {})} for k, v in items.items()
        ]
    for item in items or []:
        if isinstance(item, dict):
            key = str(item.get("path") or item.get("plan") or item.get("file") or "")
            if key:
                entries[Path(key).name] = item
    return entries


def plan_index(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Dated plans minted within N days plus active registry entries, with the tickets each names."""
    p = cast(ModelPlanPageInput, params)
    folder = _plans_dir(ctx)
    lifecycle = _lifecycle(ctx)
    plans = {name: (minted, age) for name, minted, age in _dated_plans(ctx, p.days)}
    for name, entry in lifecycle.items():
        if str(entry.get("status", "")).lower() == "active" and name not in plans:
            match = re.match(r"^(\d{4}-\d{2}-\d{2})-", name)
            if match:
                minted = match.group(1)
                age = (
                    datetime.strptime(ctx.date, "%Y-%m-%d").date()
                    - datetime.strptime(minted, "%Y-%m-%d").date()
                ).days
                plans[name] = (minted, age)
    ordered = sorted(plans.items(), key=lambda kv: kv[1][0])
    page = ordered[p.offset : p.offset + p.limit]
    rows: list[JsonValue] = []
    day = datetime.strptime(ctx.date, "%Y-%m-%d").replace(tzinfo=UTC)
    for name, (minted, age) in page:
        relative = f"{folder}/{name}"
        blob = _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{relative}") or ""
        last = _git_out(
            ctx.kb_clone,
            "log",
            "-1",
            "--format=%cI",
            "refs/remotes/origin/main",
            "--",
            relative,
        )
        changed = _parse(last)
        entry = lifecycle.get(name, {})
        ids = sorted(set(TICKET_RE.findall(blob)), key=lambda t: int(t[4:]))
        rows.append(
            {
                "plan": name,
                "minted": minted,
                "days_since_minted": age,
                "days_since_text_changed": (day - changed).days if changed else None,
                "lifecycle_status": entry.get("status"),
                "lifecycle_resolved_status": entry.get("resolved_status"),
                "cited_tickets": cast(JsonValue, ids),
            }
        )
    return {
        "plans_dir": folder,
        "days": p.days,
        "total": len(ordered),
        "offset": p.offset,
        "next_offset": p.offset + p.limit
        if p.offset + p.limit < len(ordered)
        else None,
        "lifecycle_entries": len(lifecycle),
        "plans": rows,
    }


# --- the ledger -------------------------------------------------------------------------


def _ledger_files(ctx: ModelMorningToolContext) -> list[Path]:
    archive = ctx.ledger_path.parent / "archive"
    rolled = (
        sorted(archive.glob("ROLLING_WORK_LEDGER_*.md")) if archive.is_dir() else []
    )
    return [ctx.ledger_path, *rolled]


def _cell(rest: str, key: str) -> str | None:
    match = re.search(rf"(?:^|\|)\s*{key}=([^\s|]+)", rest)
    return match.group(1) if match else None


def ledger_open_claims(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """CLAIM rows with no answering TERMINAL, CLOSEOUT or closes-CLAIM, and HOLDs not released."""
    p = cast(ModelSinceHoursInput, params)
    floor = _now() - timedelta(hours=p.since_hours)
    claims: dict[tuple[str, str], dict[str, Any]] = {}
    closed_lanes: dict[str, list[str]] = {}
    closes: set[str] = set()
    holds: dict[str, dict[str, Any]] = {}
    released: set[str] = set()
    with ctx.ledger_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            match = LEDGER_ROW_RE.match(line.rstrip("\n"))
            if not match:
                continue
            stamp, kind, rest = match.groups()
            lane = _cell(rest, "lane") or _cell(rest, "from")
            for cited in re.findall(r"closes-claim=([^\s|]+)", rest, re.I):
                closes.add(cited)
            if kind == "CLAIM" and lane:
                claims[(lane, stamp)] = {
                    "stamp": stamp,
                    "lane": lane,
                    "ticket": _cell(rest, "ticket") or _cell(rest, "tickets"),
                    "line": number,
                    "text": line.strip()[:300],
                }
            elif kind in ("TERMINAL", "CLOSEOUT") and lane:
                closed_lanes.setdefault(lane, []).append(stamp)
            elif kind == "HOLD":
                hold_id = _cell(rest, "id") or stamp
                holds[hold_id] = {
                    "stamp": stamp,
                    "id": hold_id,
                    "from": _cell(rest, "from"),
                    "to": _cell(rest, "to"),
                    "line": number,
                    "text": line.strip()[:300],
                }
            elif kind == "RELEASE":
                for key in ("re", "id", "releases"):
                    cited_id = _cell(rest, key)
                    if cited_id:
                        released.add(cited_id)
    open_rows: list[dict[str, Any]] = []
    now = _now()
    for (lane, stamp), claim in claims.items():
        when = _parse(stamp)
        if when is None or when < floor:
            continue
        answered = (
            any(s >= stamp for s in closed_lanes.get(lane, [])) or stamp in closes
        )
        if not answered:
            claim["age_hours"] = round((now - when).total_seconds() / 3600, 1)
            open_rows.append(claim)
    open_rows.sort(key=lambda c: str(c["stamp"]))
    open_holds = [
        h
        for h in holds.values()
        if h["id"] not in released and (_parse(h["stamp"]) or now) >= floor
    ]
    return {
        "ledger": ctx.ledger_path.name,
        "since": _stamp(floor),
        "open_claims_total": len(open_rows),
        "open_claims": cast(JsonValue, open_rows[:120]),
        "open_holds_total": len(open_holds),
        "open_holds": cast(JsonValue, open_holds[:40]),
    }


#: A report is named <lane>-<YYYY-MM-DDTHHMMZ>.md (COMMON brief, OMN-18397) or <lane>.md.
REPORT_STAMP_RE = re.compile(r"-\d{4}-\d{2}-\d{2}T\d{4,6}Z$")


def report_lane(stem: str) -> str:
    """The lane a report belongs to: its basename with the report-time stamp removed."""
    return REPORT_STAMP_RE.sub("", stem)


def lane_reports_index(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """UNCLOSED and ORPHANED lane reports, grepped against the live ledger AND every archive."""
    reports = ctx.place("reports_dir")
    now = time.time()
    found: list[tuple[str, str, float]] = []
    excluded = 0
    for entry in reports.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            excluded += 1
            continue
        found.append((entry.name, report_lane(entry.stem), entry.stat().st_mtime))
    lanes = {lane for _, lane, _ in found}
    names = {name for name, _, _ in found}
    terminal: dict[str, tuple[str, int]] = {}
    closeout: dict[str, tuple[str, int]] = {}
    named: set[str] = set()
    files = _ledger_files(ctx)
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                for token in TOKEN_RE.findall(line):
                    if token in lanes or token in names:
                        named.add(token)
                match = LEDGER_ROW_RE.match(line)
                kind = match.group(2) if match else None
                if kind not in ("TERMINAL", "CLOSEOUT"):
                    if " TERMINAL " not in line and " CLOSEOUT " not in line:
                        continue
                    kind = "TERMINAL" if " TERMINAL " in line else "CLOSEOUT"
                for key in ("lane", "actor"):
                    for value in re.findall(rf"{key}=([^\s|]+)", line):
                        if value in lanes:
                            target = terminal if kind == "TERMINAL" else closeout
                            target.setdefault(value, (path.name, number))
    unclosed: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    for name, lane, mtime in found:
        age_h = (now - mtime) / 3600
        if age_h <= 24 and lane not in terminal and lane not in closeout:
            unclosed.append(
                {
                    "file": name,
                    "lane": lane,
                    "age_hours": round(age_h, 1),
                    "missing": ["TERMINAL", "CLOSEOUT"],
                }
            )
        if age_h > 24 * 7 and lane not in named and name not in named:
            orphaned.append(
                {"file": name, "lane": lane, "age_days": round(age_h / 24, 1)}
            )
    unclosed.sort(key=lambda r: -float(r["age_hours"]))
    orphaned.sort(key=lambda r: -float(r["age_days"]))
    control = next(
        (
            {
                "lane": lane,
                "file": name,
                "row": "TERMINAL",
                "ledger": terminal[lane][0],
                "line": terminal[lane][1],
            }
            for name, lane, _ in sorted(found, key=lambda f: -f[2])
            if lane in terminal
        ),
        None,
    )
    return {
        "reports_scanned": len(found),
        "lanes_scanned": len(lanes),
        "non_markdown_excluded": excluded,
        "ledger_files_searched": cast(JsonValue, [p.name for p in files]),
        "unclosed_total": len(unclosed),
        "unclosed": cast(JsonValue, unclosed[:40]),
        "orphaned_total": len(orphaned),
        "orphaned": cast(JsonValue, orphaned[:40]),
        "positive_control": cast(JsonValue, control),
        "rule": (
            "lane = the report's basename less its -YYYY-MM-DDTHHMMZ report stamp; UNCLOSED: "
            "written in the last 24 h and the lane has neither a TERMINAL nor a CLOSEOUT row "
            "in any ledger file; ORPHANED: older than 7 days and no ledger row in any file "
            "names the lane or the file name."
        ),
    }


# --- this host: canonical clones and the dispatch venv ---------------------------------


def _lines(text: str | None) -> int:
    return len(text.splitlines()) if text else 0


def canonical_clone_health(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """Per canonical clone directly under the workspace: unmerged, staged, unstaged, behind."""
    root = ctx.place("clones_root")
    rows: list[dict[str, Any]] = []
    for clone in sorted(root.iterdir()):
        if not (clone / ".git").is_dir():
            continue
        unmerged = _git_out(clone, "diff", "--name-only", "--diff-filter=U")
        staged = _git_out(clone, "diff", "--cached", "--name-only")
        unstaged = _git_out(clone, "diff", "--name-only")
        behind = _git_out(clone, "rev-list", "--count", "HEAD..@{u}")
        branch = _git_out(clone, "branch", "--show-current")
        state = "WEDGED" if unmerged else ("DIRTY" if (staged or unstaged) else "CLEAN")
        rows.append(
            {
                "clone": clone.name,
                "branch": branch,
                "state": state if unmerged is not None else "UNKNOWN",
                "unmerged_paths": _lines(unmerged),
                "staged": _lines(staged),
                "unstaged": _lines(unstaged),
                "behind_upstream": int(behind) if behind and behind.isdigit() else None,
            }
        )
    rows.sort(
        key=lambda r: {"WEDGED": 0, "DIRTY": 1, "UNKNOWN": 2, "CLEAN": 3}[r["state"]]
    )
    control = (
        root
        / ".onex_state"
        / "canonical-clone-converge"
        / "omnibase_infra-20260921T103703Z"
    )
    return {
        "clones": cast(JsonValue, rows),
        "wedged": sum(r["state"] == "WEDGED" for r in rows),
        "dirty": sum(r["state"] == "DIRTY" for r in rows),
        "standing_control_manifest_present": control.is_dir(),
        "standing_control_manifest": ".onex_state/canonical-clone-converge/omnibase_infra-20260921T103703Z",
    }


def venv_reconcile_verdict(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """The dispatch venv reconciler's own status line and last tick line, with their age."""
    out: Result = {}
    status = ctx.place("venv_reconcile_status")
    log = ctx.place("venv_reconcile_log")
    now = time.time()
    try:
        out["status_line"] = (
            status.read_text(encoding="utf-8").strip().splitlines()[0][:300]
        )
        out["status_age_hours"] = round((now - status.stat().st_mtime) / 3600, 2)
    except (OSError, IndexError) as exc:
        out["status_line"] = f"UNKNOWN: {status.name}: {exc}"
    try:
        ticks = [
            line
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[
                -400:
            ]
            if "reconciler_exit=" in line
        ]
        last = ticks[-1] if ticks else ""
        out["last_tick_line"] = last[:300] or "UNKNOWN: no tick line"
        stamp = _parse(last.split(" ", 1)[0]) if last else None
        out["last_tick_age_hours"] = (
            round((_now() - stamp).total_seconds() / 3600, 2) if stamp else None
        )
        exit_code = re.search(r"reconciler_exit=(\d+)", last)
        verdict = re.search(r'verdict="([^"]*)"', last)
        out["reconciler_exit"] = int(exit_code.group(1)) if exit_code else None
        out["verdict"] = verdict.group(1) if verdict else None
        age = out["last_tick_age_hours"]
        out["stale"] = bool(isinstance(age, float) and age > 24)
    except OSError as exc:
        out["last_tick_line"] = f"UNKNOWN: {log.name}: {exc}"
    return out


READ_TOOLS: tuple[MorningTool, ...] = (
    MorningTool(
        "morning_readme",
        "The morning process README (the format contract): with no heading, its heading "
        "list; with a heading's text, that whole section.",
        ModelReadmeInput,
        "read",
        morning_readme,
    ),
    MorningTool(
        "kb_read_artifact",
        "Read one morning artifact's text by key: the run's worktree copy when it exists "
        "on the run branch, else origin/main's. Long files page by offset_lines.",
        ModelArtifactReadInput,
        "read",
        kb_read_artifact,
    ),
    MorningTool(
        "kb_source_doc_keys",
        "The source document keys this run declares (plan, must-do and off-rails docs).",
        ModelNoInput,
        "read",
        kb_source_doc_keys,
    ),
    MorningTool(
        "kb_read_source_doc",
        "Read one source document by key, resolved per SOURCE-DOC RESOLUTION (the "
        "knowledge-base copy by basename under beta/plans or beta/tracking first, then the "
        "workspace copy) and naming which copy was read and every copy checked.",
        ModelSourceDocInput,
        "read",
        kb_read_source_doc,
    ),
    MorningTool(
        "kb_read_plan",
        "Read one dated plan document by file name from the knowledge base's plans directory.",
        ModelPlanReadInput,
        "read",
        kb_read_plan,
    ),
    MorningTool(
        "plan_index",
        "Dated plans minted within N days of the run date plus active PLAN_LIFECYCLE.yaml "
        "entries, oldest first: minted date, days since minted, days since the text last "
        "changed, lifecycle status and resolved_status, and every OMN id the text names.",
        ModelPlanPageInput,
        "read",
        plan_index,
    ),
    MorningTool(
        "ledger_open_claims",
        "Un-TERMINAL-ed CLAIM rows (no later TERMINAL or CLOSEOUT for the lane and no "
        "closes-CLAIM citing them) and HOLD rows not RELEASEd, from the live ledger.",
        ModelSinceHoursInput,
        "read",
        ledger_open_claims,
    ),
    MorningTool(
        "lane_reports_index",
        "Lane reports in the orchestration reports directory, classed UNCLOSED (24 h, no "
        "TERMINAL and no CLOSEOUT) and ORPHANED (over 7 days, never named) against the live "
        "ledger and every archive roll, with a positive control row and the excluded count.",
        ModelNoInput,
        "read",
        lane_reports_index,
    ),
    MorningTool(
        "canonical_clone_health",
        "Every canonical clone under the workspace: unmerged paths (WEDGED), staged and "
        "unstaged tracked edits, commits behind upstream, plus the standing converge control.",
        ModelNoInput,
        "read",
        canonical_clone_health,
    ),
    MorningTool(
        "venv_reconcile_verdict",
        "The dispatch venv reconciler's status line and last tick line (reconciler_exit, "
        "verdict) with their ages.",
        ModelNoInput,
        "read",
        venv_reconcile_verdict,
    ),
    MorningTool(
        "watcher_branch_head",
        "From the PR watcher projection: a repository's integration branch head sha, its "
        "required contexts with a FAILING / PASSING / PENDING / ABSENT verdict over "
        "push-event check runs on that branch only, the other runs of each, and the failing "
        "push workflows at the head.",
        ModelRepoInput,
        "read",
        watcher_branch_head,
    ),
    MorningTool(
        "watcher_release_branch",
        "From the PR watcher projection: a release-synced repository's main branch head and "
        "its failing push runs (the main-sync row).",
        ModelRepoInput,
        "read",
        watcher_release_branch,
    ),
    MorningTool(
        "watcher_workflow_runs",
        "From the PR watcher projection: the newest runs of one workflow file on a "
        "repository's tracked branches (event, status, conclusion, created_at).",
        ModelWorkflowRunsInput,
        "read",
        watcher_workflow_runs,
    ),
    MorningTool(
        "workflow_triggers",
        "A repository's workflow files at its integration branch tip in the canonical clone: "
        "each workflow's triggers and job names (to tell NOT APPLICABLE from ABSENT).",
        ModelWorkflowTriggersInput,
        "read",
        workflow_triggers,
    ),
    MorningTool(
        "merged_prs_in_window",
        "Pull requests merged into a repository's integration branch since the run's window "
        "start, from the canonical clone's first-parent history joined to the watcher's "
        "merge record.",
        ModelRepoInput,
        "read",
        merged_prs_in_window,
    ),
    MorningTool(
        "pull_merge_state",
        "Whether one pull request is merged (merge sha, merged_at), open, or unknown, from "
        "the watcher's merge and open-PR records and the canonical clone's history.",
        ModelPullInput,
        "read",
        pull_merge_state,
    ),
    MorningTool(
        "ticket_prs",
        "Open and merged pull requests whose title or branch carries a ticket id.",
        ModelTicketInput,
        "read",
        ticket_prs,
    ),
)
