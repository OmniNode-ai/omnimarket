# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The morning phase tools: each a typed operation that does one thing.

No tool takes a command, an argument vector, a script, a URL or a free filesystem path.
A tool that reads or writes a knowledge-base file takes an artifact KEY the run context
declares, a tool that runs a process runs the one program the deployment overlay names for
it, with arguments it builds itself, and a write tool refuses any artifact the phase does not own
(``phase_tools.json`` ``writes``). tests/test_morning_phase_tools_guard.py holds every
input schema here to that rule.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .base import (
    BAR_ROW_RE,
    LANE_PATTERN,
    LEDGER_ROW_RE,
    ArtifactKey,
    LaneName,
    ModelNoInput,
    MorningTool,
    MorningToolRefusalError,
    Result,
    _command,
    _git,
    _git_blob,
    _git_hooked,
    _git_out,
    _proc,
    _worktree_ready,
    _writable,
)
from .context import ModelMorningToolContext
from .effects import EFFECT_TOOLS
from .linear_tools import LINEAR_TOOLS
from .local_reads import READ_TOOLS


class ModelLedgerLaneRowsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lanes: list[LaneName] = Field(
        min_length=1,
        max_length=8,
        description="ledger lane names, matched exactly against each row's lane field",
    )
    since_hours: int = Field(ge=1, le=72, description="how far back from now to read")


class ModelLedgerRowCommitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(
        min_length=12,
        max_length=240,
        pattern=r"^[^\n\r]+$",
        description="a distinctive literal substring of one ledger row",
    )


class ModelArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: ArtifactKey


class ModelArtifactsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[ArtifactKey] = Field(min_length=1, max_length=4)


class ModelKbCommitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[ArtifactKey] = Field(min_length=1, max_length=4)
    what: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[A-Za-z0-9 ,.:()'-]+$",
        description="what the commit records; the message is '<commit prefix> <what> <date>'",
    )


class ModelLedgerReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply: bool
    live_lanes: list[LaneName] | None = Field(default=None, max_length=200)


# --- read tools ------------------------------------------------------------------------


def clock_utc(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    return {"clock_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _row_lane(kind: str, cells: str) -> str | None:
    match = re.search(r"(?:^|\|)\s*lane=([^\s|]+)\s*(?:\||$)", cells)
    return match.group(1) if match else None


def ledger_lane_rows(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelLedgerLaneRowsInput, params)
    for name in p.lanes:
        if not re.fullmatch(LANE_PATTERN, name):
            raise MorningToolRefusalError(f"not a lane name: {name!r}")
    wanted = set(p.lanes)
    floor = datetime.now(UTC) - timedelta(hours=p.since_hours)
    rows: list[JsonValue] = []
    scanned = 0
    with ctx.ledger_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.rstrip("\n")
            match = LEDGER_ROW_RE.match(line)
            lane: str | None
            closes: re.Match[str] | None = None
            if match:
                stamp, kind, rest = match.groups()
                lane = _row_lane(kind, rest)
                closes = re.search(r"closes-claim=([^|]+)", rest, re.I)
            else:
                bar = BAR_ROW_RE.match(line)
                if not bar:
                    continue
                stamp, lane_cell, rest = bar.groups()
                lane = lane_cell.strip()
                cells = [c.strip() for c in rest.split("|")]
                kind = next(
                    (c for c in cells if c in ("CLAIM", "TERMINAL", "CLOSEOUT")), ""
                )
            scanned += 1
            try:
                when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            if when < floor:
                continue
            cites = any(
                re.search(rf"lane={re.escape(name)}(?=[\s|]|$)", rest)
                for name in wanted
            )
            if lane in wanted and kind in ("CLAIM", "TERMINAL", "CLOSEOUT"):
                pass
            elif not (cites and (closes or "supersedes-claim=" in rest)):
                continue
            rows.append(
                {
                    "line": number,
                    "stamp": stamp,
                    "type": kind,
                    "lane": lane,
                    "closes_claim": closes.group(1).strip() if closes else None,
                    "text": line[:600],
                }
            )
    return {
        "ledger": ctx.ledger_path.name,
        "rows_scanned": scanned,
        "since": floor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows,
    }


def ledger_row_commit(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelLedgerRowCommitInput, params)
    repo = Path(_git_out(ctx.ledger_path.parent, "rev-parse", "--show-toplevel") or "")
    if not repo.is_dir():
        raise MorningToolRefusalError(
            f"the ledger {ctx.ledger_path} is not in a git checkout"
        )
    relative = str(ctx.ledger_path.resolve().relative_to(repo.resolve()))
    sha = _git_out(repo, "log", "-1", "--format=%H", f"-S{p.text}", "--", relative)
    status = _git_out(repo, "status", "--porcelain", "--", relative)
    return {
        "commit_sha": sha or "",
        "found": bool(sha),
        "ledger_clean": status == "",
        "ledger_status": status if status is not None else "UNREADABLE",
    }


def _headings(text: str) -> list[JsonValue]:
    found: list[JsonValue] = [
        line[:160] for line in text.splitlines() if line.startswith("#")
    ]
    return found[:80]


def _header_keys(text: str) -> dict[str, JsonValue]:
    keys: dict[str, JsonValue] = {}
    for line in text.splitlines()[:30]:
        match = re.match(r"^([a-z_]+):\s?(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2)[:200]
    return keys


def _table_headers(text: str) -> list[JsonValue]:
    """The header row of every markdown table (a '|' row followed by a '|---' row)."""
    lines = text.splitlines()
    found: list[JsonValue] = [
        lines[i][:240]
        for i in range(len(lines) - 1)
        if lines[i].lstrip().startswith("|")
        and re.match(r"^\s*\|\s*:?-{3,}", lines[i + 1])
    ]
    return found[:60]


def _describe(text: str, relative: str) -> Result:
    described: Result = {
        "line_count": len(text.splitlines()),
        "headings": _headings(text),
        "header_keys": _header_keys(text),
        "table_headers": _table_headers(text),
    }
    if relative.endswith(".json"):
        try:
            loaded = json.loads(text)
            described["json_as_of"] = (
                loaded.get("as_of") if isinstance(loaded, dict) else None
            )
        except ValueError as exc:
            described["json_error"] = str(exc)
    return described


def kb_artifact_status(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelArtifactInput, params)
    relative = ctx.artifact_path(p.artifact)
    locations: dict[str, JsonValue] = {}
    worktree = ctx.kb_worktree
    branch = (
        _git_out(worktree, "branch", "--show-current") if worktree.is_dir() else None
    )
    if branch is None:
        locations["worktree"] = {"present": False, "path_checked": str(worktree)}
    else:
        file = worktree / relative
        log = _git_out(worktree, "log", "-3", "--format=%H", "--", relative) or ""
        status = _git_out(worktree, "status", "--porcelain", "--", relative)
        entry: Result = {
            "present": True,
            "branch": branch,
            "on_run_branch": branch == ctx.kb_branch,
            "exists": file.is_file(),
            "commits": cast(JsonValue, log.split()),
            "clean": status == "",
            "status": status if status is not None else "UNREADABLE",
        }
        if file.is_file():
            entry.update(
                _describe(file.read_text(encoding="utf-8", errors="replace"), relative)
            )
        locations["worktree"] = entry
    blob = _git_blob(ctx.kb_clone, f"refs/remotes/origin/main:{relative}")
    main_entry: Result = {"exists": blob is not None}
    if blob is not None:
        main_entry.update(_describe(blob, relative))
        main_entry["commits"] = cast(
            JsonValue,
            (
                _git_out(
                    ctx.kb_clone,
                    "log",
                    "-3",
                    "--format=%H",
                    "refs/remotes/origin/main",
                    "--",
                    relative,
                )
                or ""
            ).split(),
        )
    locations["origin_main"] = main_entry
    return {
        "artifact": p.artifact,
        "path": relative,
        "date": ctx.date,
        "locations": locations,
    }


# --- knowledge-base worktree, scrub and commit -------------------------------------------


def kb_worktree_prepare(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """WORKTREE SETUP (OMN-18071), as one deterministic operation."""
    clone, kb, branch = ctx.kb_clone, ctx.kb_worktree, ctx.kb_branch
    if _git_out(clone, "rev-parse", "--show-toplevel") is None:
        raise MorningToolRefusalError(
            f"the knowledge-base clone {clone} is not a git clone"
        )
    origin = _git_out(clone, "remote", "get-url", "origin") or ""
    if not ctx.kb_origin_pattern or not re.search(ctx.kb_origin_pattern, origin):
        raise MorningToolRefusalError(
            f"{clone} is not the deployment's knowledge-base clone (origin {origin!r})"
        )
    refreshed = _proc(
        ctx.command("kb_clone_refresh_command"), cwd=ctx.root, timeout=300
    )
    if refreshed["exit"] != 0:
        raise MorningToolRefusalError(
            f"canonical clone refresh failed: {refreshed['stderr']}"
        )
    base = _git_out(clone, "rev-parse", "refs/remotes/origin/main")
    if not base:
        raise MorningToolRefusalError(
            "cannot resolve refs/remotes/origin/main in the clone"
        )
    if kb.exists():
        common = _git_out(kb, "rev-parse", "--path-format=absolute", "--git-common-dir")
        clone_common = _git_out(
            clone, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        top = _git_out(kb, "rev-parse", "--show-toplevel")
        if not ((kb / ".git").is_file() and top == str(kb) and common == clone_common):
            raise MorningToolRefusalError(
                "BLOCKED: existing path is not this clone's linked worktree"
            )
        dirty = _git_out(kb, "status", "--porcelain", "--untracked-files=all")
        if dirty is None or dirty:
            raise MorningToolRefusalError(
                "BLOCKED: existing worktree is dirty; preserve it and report its owner"
            )
        current = _git_out(kb, "branch", "--show-current")
        action = "reused"
        if current != branch:
            if _git(kb, "merge-base", "--is-ancestor", "HEAD", base)["exit"] != 0:
                raise MorningToolRefusalError(
                    "BLOCKED: prior branch has unmerged work; preserve it"
                )
            target_exists = _git(
                kb, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
            )
            if (
                target_exists["exit"] == 0
                and _git(kb, "merge-base", "--is-ancestor", branch, base)["exit"] != 0
            ):
                raise MorningToolRefusalError(
                    "BLOCKED: target branch has unmerged work"
                )
            switched = _git(kb, "switch", "-C", branch, base)
            if switched["exit"] != 0:
                raise MorningToolRefusalError(
                    f"git switch failed: {switched['stderr']}"
                )
            action = "rebranched"
    else:
        kb.parent.mkdir(parents=True, exist_ok=True)
        added = _git(clone, "worktree", "add", str(kb), "-b", branch, base)
        if added["exit"] != 0:
            raise MorningToolRefusalError(f"git worktree add failed: {added['stderr']}")
        action = "created"
    return {"worktree": str(kb), "branch": branch, "base": base, "action": action}


def kb_scrub(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelArtifactsInput, params)
    _worktree_ready(ctx)
    paths = _writable(ctx, list(p.artifacts))
    missing = [path for path in paths if not (ctx.kb_worktree / path).is_file()]
    if missing:
        raise MorningToolRefusalError(f"nothing to scrub, not written yet: {missing}")
    done = _command(
        ctx,
        "shared_doc_scrub_command",
        "--write",
        *[str(ctx.kb_worktree / path) for path in paths],
    )
    meaning = {
        0: "clean or repaired",
        1: "local path with no portable form",
        2: "REFUSED",
    }
    return {**done, "meaning": meaning.get(cast(int, done["exit"]), "unexpected exit")}


def kb_commit(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelKbCommitInput, params)
    _worktree_ready(ctx)
    paths = _writable(ctx, list(p.artifacts))
    added = _git(ctx.kb_worktree, "add", "--", *paths)
    if added["exit"] != 0:
        raise MorningToolRefusalError(f"git add failed: {added['stderr']}")
    changed = _git_out(ctx.kb_worktree, "diff", "--cached", "--name-only", "--", *paths)
    if not changed:
        head = _git_out(ctx.kb_worktree, "rev-parse", "HEAD") or ""
        return {
            "committed": False,
            "reason": "no change in these paths",
            "commit_sha": head,
        }
    message = f"{ctx.commit_prefix} {p.what} {ctx.date}"
    done = _git_hooked(ctx.kb_worktree, "commit", "--only", "-m", message, "--", *paths)
    for _ in range(3):  # a concurrent phase's commit holds the index lock for a moment
        if done["exit"] == 0 or "index.lock" not in str(done["stderr"]):
            break
        time.sleep(3)
        done = _git_hooked(
            ctx.kb_worktree, "commit", "--only", "-m", message, "--", *paths
        )
    if done["exit"] != 0:
        return {
            "committed": False,
            "reason": "commit refused (hook output verbatim)",
            **done,
        }
    return {
        "committed": True,
        "commit_sha": _git_out(ctx.kb_worktree, "rev-parse", "HEAD") or "",
        "message": message,
        "paths": cast(JsonValue, paths),
    }


# --- decisions register (OMN-18575) ------------------------------------------------------


def _decisions_args(ctx: ModelMorningToolContext) -> list[str]:
    return [
        "--ledger",
        str(ctx.ledger_path),
        "--archive-dir",
        str(ctx.ledger_path.parent / "archive"),
        "--stale-days",
        str(ctx.decisions_stale_days),
    ]


def decisions_register_generate(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    _worktree_ready(ctx)
    paths = _writable(ctx, ["decisions_md", "decisions_json"])
    done = _command(
        ctx,
        "decisions_register_command",
        *_decisions_args(ctx),
        "--out-md",
        str(ctx.kb_worktree / paths[0]),
        "--out-json",
        str(ctx.kb_worktree / paths[1]),
    )
    text = f"{done['stdout']}\n{done['stderr']}"
    lines = text.splitlines()
    counts = next((x for x in lines if x.startswith("[decisions] total=")), "")
    oldest = next((x for x in lines if x.startswith("[decisions] oldest pending")), "")
    return {**done, "counts_line": counts, "oldest_pending_line": oldest}


def decisions_register_check(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    done = _command(ctx, "decisions_register_command", *_decisions_args(ctx), "--check")
    return {
        "alarm_exit": done["exit"],
        "stdout": done["stdout"],
        "stderr": done["stderr"],
    }


def decisions_asked(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    _worktree_ready(ctx)
    path = ctx.kb_worktree / ctx.artifact_path("decisions_json")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    entries = loaded.get("decisions", []) if isinstance(loaded, dict) else loaded
    asked = [
        {
            "decided_at": e.get("decided_at", ""),
            "asked_of": e.get("asked_of") or e.get("owner", ""),
            "question": e.get("question") or e.get("summary", ""),
            "id": e.get("id", ""),
        }
        for e in entries
        if isinstance(e, dict) and e.get("status") == "ASKED"
    ]
    asked.sort(key=lambda e: str(e["decided_at"]))
    lines = [
        f"{e['decided_at']} asked_of={e['asked_of']}: {e['question']}" for e in asked
    ]
    return {
        "total": len(entries),
        "asked_count": len(asked),
        "asked": cast(JsonValue, asked),
        "notes_lines": cast(JsonValue, lines),
    }


# --- ledger reconcile (run by the node itself, no agent) --------------------------------


def ledger_reconcile(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelLedgerReconcileInput, params)
    args = [
        "--apply" if p.apply else "--report",
        "--stale-hours",
        "6",
        "--since-days",
        "0",
        "--max-appends",
        "60",
        "--json",
    ]
    for lane in p.live_lanes or []:
        if not re.fullmatch(LANE_PATTERN, lane):
            raise MorningToolRefusalError(f"not a lane name: {lane!r}")
        args += ["--live-lane", lane]
    if p.live_lanes is not None:
        args += ["--live-roster", "--silent-hours", "72"]
    return _command(ctx, "ledger_reconcile_command", *args)


TOOLS: dict[str, MorningTool] = {
    tool.name: tool
    for tool in (
        MorningTool(
            "clock_utc", "Read the UTC clock once.", ModelNoInput, "read", clock_utc
        ),
        MorningTool(
            "ledger_lane_rows",
            "The CLAIM, TERMINAL and CLOSEOUT rows of the named lanes in the rolling work "
            "ledger since a bound, plus later rows citing them by closes-CLAIM.",
            ModelLedgerLaneRowsInput,
            "read",
            ledger_lane_rows,
        ),
        MorningTool(
            "ledger_row_commit",
            "The commit that introduced a ledger row (git log -S on the ledger) and "
            "whether the ledger file is committed and clean.",
            ModelLedgerRowCommitInput,
            "read",
            ledger_row_commit,
        ),
        MorningTool(
            "kb_artifact_status",
            "For one morning artifact: in the run's knowledge-base worktree and on "
            "origin/main, whether it exists, the commits touching it, whether it is "
            "clean, its line count, headings and header keys.",
            ModelArtifactInput,
            "read",
            kb_artifact_status,
        ),
        MorningTool(
            "kb_worktree_prepare",
            "Refresh the knowledge-base clone and create or reuse the run's isolated "
            "worktree on its dated branch; refuses dirty, foreign or unmerged state.",
            ModelNoInput,
            "write",
            kb_worktree_prepare,
        ),
        MorningTool(
            "kb_scrub",
            "Run the shared-document scrub (--write) on artifacts this phase wrote.",
            ModelArtifactsInput,
            "write",
            kb_scrub,
        ),
        MorningTool(
            "kb_commit",
            "Commit exactly the named artifacts this phase owns in the run's "
            "knowledge-base worktree. Never pushes.",
            ModelKbCommitInput,
            "write",
            kb_commit,
        ),
        MorningTool(
            "decisions_register_generate",
            "Generate the decisions register (markdown and JSON) into the worktree.",
            ModelNoInput,
            "write",
            decisions_register_generate,
        ),
        MorningTool(
            "decisions_register_check",
            "Run the decisions register forgetting alarm (--check); returns its exit.",
            ModelNoInput,
            "read",
            decisions_register_check,
        ),
        MorningTool(
            "decisions_asked",
            "The ASKED decisions in the register this run wrote, oldest first.",
            ModelNoInput,
            "read",
            decisions_asked,
        ),
        MorningTool(
            "ledger_reconcile",
            "Run the ledger reconciliation node once.",
            ModelLedgerReconcileInput,
            "write",
            ledger_reconcile,
        ),
        *READ_TOOLS,
        *LINEAR_TOOLS,
        *EFFECT_TOOLS,
    )
}
