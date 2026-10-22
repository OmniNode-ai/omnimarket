# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The morning tools with an effect: an artifact write, a ledger row, a delegation step, a
committed reader or verifier run, and the publishing phase's push, PR and handoff.

Each runs one program the deployment overlay names, with arguments the tool builds from the
run context and its typed input. The agent never supplies a command, a path, a lane, a
ticket or a branch: the phase's lane and ticket come from ``phase_tools.json``, every place
from the overlay. Publishing (push, PR, handoff) refuses in a dry run.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from .base import (
    ArtifactKey,
    ModelNoInput,
    MorningTool,
    MorningToolRefusalError,
    Result,
    _command,
    _git_hooked,
    _git_out,
    _proc,
    _tail,
    _worktree_ready,
    _writable,
)
from .context import ModelMorningToolContext
from .local_reads import ModelTicketInput, TicketId

#: Line caps of the format contract, per artifact (reported, and refused past them).
LINE_CAPS = {
    "ground_state": 200,
    "rebaseline": 250,
    "integration_plan": 250,
    "dropped_work": 250,
    "goal": 89,
}
CELL_TEXT = r"^[^|\n\r\x00]+$"
CellText = Annotated[str, StringConstraints(pattern=CELL_TEXT)]
SLUG = r"^[a-z][a-z0-9-]{1,40}$"
#: Verifier runs per phase session: each takes up to minutes, and the phase has an hour.
DOD_VERIFY_BUDGET = 20


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lane(ctx: ModelMorningToolContext) -> tuple[str, str]:
    if not ctx.lane or not ctx.ticket:
        raise MorningToolRefusalError(
            f"phase {ctx.phase_label} has no ledger lane and ticket in phase_tools.json"
        )
    return ctx.lane, ctx.ticket


def _publish_only(ctx: ModelMorningToolContext, what: str) -> None:
    if not ctx.publish:
        raise MorningToolRefusalError(
            f"DRY run: {what} is not performed; return publish_state SKIPPED and name it"
        )


# --- the phase's own artifact ------------------------------------------------------------


class ModelArtifactWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: ArtifactKey
    content: str = Field(
        min_length=1,
        max_length=160_000,
        pattern=r"^[^\x00]*$",
        description="the whole file; it replaces the worktree copy",
    )


def kb_write_artifact(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Write one artifact this phase owns, whole, into the run's knowledge-base worktree."""
    p = cast(ModelArtifactWriteInput, params)
    _worktree_ready(ctx)
    (relative,) = _writable(ctx, [p.artifact])
    lines = p.content.splitlines()
    cap = LINE_CAPS.get(p.artifact)
    if cap is not None and len(lines) > cap:
        raise MorningToolRefusalError(
            f"{relative} would be {len(lines)} lines; the format contract caps it at {cap}"
        )
    target = ctx.kb_worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    text = p.content if p.content.endswith("\n") else p.content + "\n"
    target.write_text(text, encoding="utf-8")
    return {
        "artifact": p.artifact,
        "path": relative,
        "lines": len(lines),
        "line_cap": cap,
        "bytes": len(text.encode()),
        "next": "kb_scrub, then kb_commit",
    }


# --- delegation (OMN-17009): the prose is drafted by the delegation chain ------------------


def _registry(
    ctx: ModelMorningToolContext,
) -> tuple[Path, dict[str, dict[str, JsonValue]]]:
    folder = ctx.state() / f"delegation-{ctx.phase_label}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "registry.json"
    loaded = json.loads(path.read_text()) if path.is_file() else {}
    return path, cast(dict[str, dict[str, JsonValue]], loaded)


class ModelDelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str = Field(
        pattern=SLUG, description="a short slug naming the prose, e.g. section-4"
    )
    prompt: str = Field(
        min_length=40,
        max_length=40_000,
        pattern=r"^[^\x00]*$",
        description="the fact table and what to write around it, facts only",
    )


def delegate_prose(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """Draft one piece of prose through the delegation chain (GLM, then the lab), receipted."""
    p = cast(ModelDelegateInput, params)
    lane, _ = _lane(ctx)
    path, registry = _registry(ctx)
    draft_id = str(len(registry) + 1)
    folder = path.parent
    prompt_file = folder / f"{draft_id}-{p.step}.prompt.md"
    out_file = folder / f"{draft_id}-{p.step}.draft.md"
    prompt_file.write_text(p.prompt, encoding="utf-8")
    done = _proc(
        [
            *ctx.command("delegation_step_command"),
            "text",
            "--lane",
            lane,
            "--step",
            p.step,
            "--prompt-file",
            str(prompt_file),
            "--out",
            str(out_file),
            "--ticket",
            "OMN-17009",
        ],
        cwd=ctx.root,
        timeout=700,
    )
    text = str(done["stdout"])
    receipts = re.findall(r"^RECEIPT (\S+)$", text, re.M)
    delegated = re.search(r"^DELEGATED step=\S+ engine=(\S+) run=(\S+)", text, re.M)
    registry[draft_id] = {
        "step": p.step,
        "receipts": cast(JsonValue, receipts),
        "engine": delegated.group(1) if delegated else None,
        "run": delegated.group(2) if delegated else None,
        "ok": bool(delegated) and out_file.is_file(),
        "disposed": False,
    }
    path.write_text(json.dumps(registry, indent=2))
    if not registry[draft_id]["ok"]:
        return {
            "draft_id": draft_id,
            "ok": False,
            "exit": done["exit"],
            "output": _tail(text + "\n" + str(done["stderr"])),
            "receipts": cast(JsonValue, receipts),
        }
    return {
        "draft_id": draft_id,
        "ok": True,
        "engine": registry[draft_id]["engine"],
        "run": registry[draft_id]["run"],
        "draft": out_file.read_text(encoding="utf-8")[:30000],
        "next": "fact-check the draft against your fact table, then delegate_dispose",
    }


#: The delegation step's reason codes per disposition (delegation_step.py DISPOSITION_REASONS).
DISPOSITION_REASONS = {
    "used": ("correct_as_is", "verified_against_source"),
    "edited": ("minor_fix", "partial_use", "reformatted"),
    "discarded": (
        "hallucinated",
        "incomplete",
        "off_task",
        "unusable_format",
        "wrong_answer",
    ),
    "ignored": ("engine_failed", "lane_decided_first", "not_needed", "superseded"),
}


class ModelDisposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(pattern=r"^\d{1,4}$")
    disposition: Literal["used", "edited", "discarded", "ignored"]
    reason_code: Literal[
        "correct_as_is",
        "verified_against_source",
        "minor_fix",
        "partial_use",
        "reformatted",
        "hallucinated",
        "incomplete",
        "off_task",
        "unusable_format",
        "wrong_answer",
        "engine_failed",
        "lane_decided_first",
        "not_needed",
        "superseded",
    ] = Field(
        description="used: correct_as_is|verified_against_source; edited: minor_fix|partial_use|"
        "reformatted; discarded: hallucinated|incomplete|off_task|unusable_format|wrong_answer; "
        "ignored: engine_failed|lane_decided_first|not_needed|superseded"
    )
    landed_in: ArtifactKey | TicketId | Literal["ledger-terminal", "pr-body"] | None = (
        Field(
            default=None,
            description="for used or edited: the artifact key, the ticket commented on, "
            "ledger-terminal or pr-body where the text landed",
        )
    )
    final_text: str | None = Field(
        default=None,
        max_length=40_000,
        pattern=r"^[^\x00]*$",
        description="for edited: the text as used, so the edit ratio is measured against "
        "the draft; omitted, an artifact's written file is the final text",
    )


def delegate_dispose(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    p = cast(ModelDisposeInput, params)
    if p.reason_code not in DISPOSITION_REASONS[p.disposition]:
        raise MorningToolRefusalError(
            f"{p.reason_code} is not a reason for {p.disposition}; "
            f"use one of {DISPOSITION_REASONS[p.disposition]}"
        )
    path, registry = _registry(ctx)
    entry = registry.get(p.draft_id)
    if entry is None:
        raise MorningToolRefusalError(
            f"no draft {p.draft_id}; drafts: {sorted(registry)}"
        )
    receipts = cast(list[str], entry.get("receipts") or [])
    if not receipts:
        raise MorningToolRefusalError(f"draft {p.draft_id} left no receipt to dispose")
    argv = [
        *ctx.command("delegation_step_command"),
        "dispose",
        "--receipt",
        receipts[-1],
        "--disposition",
        p.disposition,
        "--reason-code",
        p.reason_code,
    ]
    if p.disposition in ("used", "edited"):
        if p.landed_in is None:
            raise MorningToolRefusalError("a used or edited draft names landed_in")
        if p.landed_in in ctx.artifacts:
            ref = f"doc:{ctx.artifact_path(p.landed_in)}"
        elif p.landed_in.startswith("OMN-"):
            ref = f"doc:linear:{p.landed_in}"
        elif p.landed_in == "pr-body":
            ref = f"doc:kb-pr:{ctx.kb_branch}"
        else:
            ref = f"doc:ledger:{ctx.lane}:{ctx.date}"
        argv += ["--artifact", ref]
    if p.disposition == "edited":
        final = path.parent / f"{p.draft_id}.final.md"
        written = (
            ctx.kb_worktree / ctx.artifact_path(p.landed_in)
            if p.landed_in in ctx.artifacts
            else None
        )
        if p.final_text is not None:
            final.write_text(p.final_text, encoding="utf-8")
        elif written is not None and written.is_file():
            final.write_text(written.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            raise MorningToolRefusalError(
                "an edited draft needs final_text (the text as used) unless it landed in an "
                "artifact this run already wrote"
            )
        argv += ["--final-file", str(final)]
    done = _proc(argv, cwd=ctx.root, timeout=120)
    # The receipt is disposed once the step prints DISPOSED; a failed publish of the
    # disposition event afterwards (DISPOSITION-NOT-PUBLISHED) does not undo that.
    recorded = done["exit"] == 0 or str(done["stdout"]).startswith("DISPOSED ")
    if recorded:
        entry["disposed"] = True
        path.write_text(json.dumps(registry, indent=2))
    return {
        "draft_id": p.draft_id,
        "disposition": p.disposition,
        "recorded": recorded,
        **done,
    }


def _cells(ctx: ModelMorningToolContext) -> Result:
    lane, _ = _lane(ctx)
    claim = _claim(ctx)
    args = ["cells", "--lane", lane]
    if claim:
        args += ["--since", claim]
    done = _proc(
        [*ctx.command("delegation_step_command"), *args], cwd=ctx.root, timeout=120
    )
    line = str(done["stdout"]).strip().splitlines()
    first = line[0] if line else ""
    count = re.search(r"\bdelegated=(\d+)", first)
    runs = re.search(r"\bruns=(\S+)", first)
    if first.startswith("UNDISPOSED"):
        return {
            "cell": None,
            "ledger_cells": None,
            "undisposed": True,
            "cells_output": _tail(str(done["stdout"])),
            "next": "delegate_dispose every draft first",
        }
    if done["exit"] == 0 and count and int(count.group(1)) > 0 and runs:
        cell: Result = {
            "delegated": int(count.group(1)),
            "runs": cast(JsonValue, runs.group(1).split(",")),
            "reason": "",
        }
        ledger = first
    else:
        _, registry = _registry(ctx)
        failed = [e for e in registry.values() if not e.get("ok")]
        token = "delegation-step-failed" if failed else "no-delegation-run"
        cell = {"delegated": 0, "runs": [], "reason": f"route-unavailable:{token}"}
        ledger = f"delegated=0 delegation_reason=route-unavailable:{token}"
    return {
        "cell": cell,
        "ledger_cells": ledger,
        "cells_output": _tail(str(done["stdout"])),
    }


def delegation_cells(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """The delegation cell this phase's receipts back, for the result and the TERMINAL."""
    return _cells(ctx)


# --- ledger CLAIM and TERMINAL -------------------------------------------------------------


def _claim_file(ctx: ModelMorningToolContext) -> Path:
    return ctx.state() / f"claim-{ctx.phase_label}.json"


def _claim(ctx: ModelMorningToolContext) -> str | None:
    path = _claim_file(ctx)
    return str(json.loads(path.read_text())["stamp"]) if path.is_file() else None


#: How much of the ledger's refusal the agent sees. The ledger prints its reason first and then
#: echoes the whole row, so the head carries the reason and the tail only the row the agent sent.
REFUSAL_CHARS = 1200
#: The ledger's quantitative-claim lint (OMN-15897) refuses a count with no citation beside it.
COUNT_LINT = "quantitative-claim"


def _refusal(done: Result) -> str:
    """The ledger's own reason for refusing a row, from the head of its output."""
    text = " ".join(
        str(done[key]).strip() for key in ("stderr", "stdout") if str(done[key]).strip()
    )
    reason = (
        f"onex-ledger refused the row (exit {done['exit']}): {text[:REFUSAL_CHARS]}"
    )
    if COUNT_LINT in text:
        reason += (
            " FIX: put each count next to the citation that backs it (repo#n, a commit sha, an "
            "OMN ticket or a run id) or leave the count out; the artifact carries the numbers."
        )
    return reason


def _append(ctx: ModelMorningToolContext, row: str) -> Result:
    done = _proc(
        [*ctx.command("ledger_append_command"), row], cwd=ctx.root, timeout=330
    )
    if done["exit"] != 0:
        raise MorningToolRefusalError(_refusal(done))
    return done


def _actor(ctx: ModelMorningToolContext) -> str:
    model = (
        re.sub(r"[^a-z0-9.-]", "", ctx.model.lower()).removeprefix("claude-")
        or "claude"
    )
    return f"actor=claude:{model}:morning-phase | model={ctx.model or 'claude'}"


def _worktree_cell(ctx: ModelMorningToolContext) -> str:
    try:
        return str(ctx.kb_worktree.relative_to(ctx.root))
    except ValueError:
        return ctx.kb_worktree.name


class ModelClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    est_hours: float = Field(ge=0.1, le=8)
    displaces: CellText = Field(max_length=120)
    what: CellText = Field(min_length=12, max_length=400)


def ledger_append_claim(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The phase's CLAIM row, on its own lane and ticket, once per run."""
    p = cast(ModelClaimInput, params)
    lane, ticket = _lane(ctx)
    if _claim(ctx):
        raise MorningToolRefusalError(f"this phase already claimed at {_claim(ctx)}")
    stamp = _now()
    row = (
        f"{stamp} | CLAIM | lane={lane} | ticket={ticket} | {_actor(ctx)} | "
        f"worktree={_worktree_cell(ctx)} | date={ctx.date} | "
        f"est ~{p.est_hours:g} lane-hours; displaces {p.displaces}; ({ticket}) | {p.what}"
    )
    _append(ctx, row)
    _claim_file(ctx).write_text(json.dumps({"stamp": stamp, "lane": lane}))
    return {"claimed": stamp, "lane": lane, "ticket": ticket}


class ModelTerminalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["done", "partial", "blocked", "failed"]
    summary: CellText = Field(
        min_length=12,
        max_length=900,
        description=(
            "what the phase delivered; the ledger refuses a bare count, so each number sits "
            "next to its citation (repo#n, a commit sha, an OMN ticket or a run id) or is left "
            "to the artifact"
        ),
    )
    friction: CellText | None = Field(
        default=None,
        max_length=600,
        description="one friction this phase hit; the tool writes its FRICTION row first",
    )
    friction_cost_minutes: int = Field(default=5, ge=1, le=240)
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")


def ledger_append_terminal(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The phase's TERMINAL row, closing its CLAIM, with the delegation cells its receipts back."""
    p = cast(ModelTerminalInput, params)
    lane, ticket = _lane(ctx)
    claim = _claim(ctx)
    if not claim:
        raise MorningToolRefusalError(
            "no CLAIM from this phase yet: ledger_append_claim first"
        )
    friction = "none"
    if p.friction:
        # A TERMINAL the ledger refused is sent again; its FRICTION row is written once.
        written = ctx.state() / f"friction-{ctx.phase_label}.json"
        seen = json.loads(written.read_text()) if written.is_file() else {}
        stamp = str(seen.get("stamp", "")) if seen.get("text") == p.friction else ""
        if not stamp:
            stamp = _now()
            _append(
                ctx,
                f"{stamp} | FRICTION | lane={lane} | ticket={ticket} | "
                f"cost=~{p.friction_cost_minutes} lane-minutes | {p.friction}",
            )
            written.write_text(json.dumps({"text": p.friction, "stamp": stamp}))
        friction = f"FRICTION {stamp}"
    cells = _cells(ctx)
    if cells.get("undisposed"):
        raise MorningToolRefusalError(
            "a delegated draft is not disposed: delegate_dispose it before the TERMINAL"
        )
    commit = f" | commit={p.commit_sha}" if p.commit_sha else ""
    row = (
        f"{_now()} | TERMINAL | lane={lane} | ticket={ticket} | {_actor(ctx)} | "
        f"friction={friction} | closes-CLAIM={claim} | outcome={p.outcome} | date={ctx.date}"
        f"{commit} | worktree=kept:{_worktree_cell(ctx)}:goal-phase-pushes-it | "
        f"{cells['ledger_cells']} | "
        f"{p.summary}"
    )
    _append(ctx, row)
    return {
        "terminal": "appended",
        "closes_claim": claim,
        "delegation_cells": cells["ledger_cells"],
    }


# --- committed readers and verifiers ----------------------------------------------------------


def delegation_accounting(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """Run the delegation accounting reader once and save its output as the dated companion."""
    _worktree_ready(ctx)
    (relative,) = _writable(ctx, ["delegation_accounting"])
    until = _now()
    since = f"{ctx.window_start or ctx.date}T00:00:00Z"
    state = ctx.place("state_root")
    args = [
        "--ledger",
        str(ctx.ledger_path),
        "--include-archive",
        "--runs-root",
        str(state / "runs"),
        "--since",
        since,
        "--until",
        until,
        "--format",
        "md",
    ]
    harness = state / "harness-delegate"
    sources = {"runs_root": "$ONEX_STATE_DIR/runs"}
    if harness.is_dir():
        args += ["--harness-root", str(harness)]
        sources["harness_root"] = "$ONEX_STATE_DIR/harness-delegate"
    command = "onex-delegation-accounting " + " ".join(
        a if not a.startswith("/") else "<" + Path(a).name + ">" for a in args
    )
    whole = _whole(ctx, "delegation_accounting_command", args, timeout=900)
    if whole.returncode != 0:
        return {
            "status": "UNREADABLE",
            "command": command,
            "exit": whole.returncode,
            "stderr": _tail(whole.stderr),
        }
    (ctx.kb_worktree / relative).parent.mkdir(parents=True, exist_ok=True)
    (ctx.kb_worktree / relative).write_text(whole.stdout, encoding="utf-8")
    return {
        "status": "READ",
        "command": command,
        "window": f"{since}..{until}",
        "sources": cast(JsonValue, sources),
        "saved_as": relative,
        "lines": len(whole.stdout.splitlines()),
        "output": whole.stdout[:24000],
        "next": "kb_scrub and kb_commit it with the report",
    }


def closeout_completion_row(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """The launchd dod-closeout-sweep completion row (section 6b), verbatim."""
    done = _command(
        ctx,
        "closeout_completion_command",
        "--ledger",
        str(ctx.ledger_path),
        "--ground-state",
    )
    return {
        "command": "handler_closeout_completion --ledger <ledger> --ground-state",
        **done,
    }


def closeout_scope_verify(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """Section 6a: the closeout verifier once on the run's fixed pin, timed."""
    ticket = ctx.closure_probe_ticket
    if not re.fullmatch(r"OMN-\d{1,6}", ticket or ""):
        raise MorningToolRefusalError("the run carries no closure probe ticket")
    started = time.monotonic()
    done = _proc(
        [
            *ctx.command("closeout_scope_command"),
            "verify",
            ticket,
            "--execution-audience",
            "hosted",
        ],
        cwd=ctx.root,
        timeout=ctx.closure_probe_ceiling_s + 120,
    )
    wall = round(time.monotonic() - started, 1)
    return {
        "ticket": ticket,
        "wall_seconds": wall,
        "ceiling_seconds": ctx.closure_probe_ceiling_s,
        "over_ceiling": wall >= ctx.closure_probe_ceiling_s,
        "command": f"handler_closeout_scope verify {ticket} --execution-audience hosted",
        **done,
    }


def _whole(
    ctx: ModelMorningToolContext, name: str, args: list[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run an overlay program and keep its whole stdout (a reader whose output is the data)."""
    try:
        return subprocess.run(
            [*ctx.command(name), *args],
            cwd=ctx.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"timed out after {timeout} s"
        )


def triage_fences(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """The fence list, derived mechanically from the ledger and the watcher's open PRs."""
    done = _whole(
        ctx, "triage_fences_command", [str(ctx.ledger_path), "--open-prs"], timeout=300
    )
    parsed: JsonValue = None
    try:
        parsed = cast(JsonValue, json.loads(done.stdout))
    except ValueError:
        parsed = None
    fenced = parsed.get("fenced_tickets") if isinstance(parsed, dict) else None
    return {
        "exit": done.returncode,
        "fenced_tickets": fenced,
        "result": parsed if len(done.stdout) < 30000 else None,
        "stdout_tail": None if parsed is not None else _tail(done.stdout),
        "stderr": _tail(done.stderr),
    }


class ModelTicketsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tickets: list[TicketId] = Field(min_length=1, max_length=60)


def triage_started(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """STARTED / UNSTARTED / EXTERNAL / PARENT per ticket (worktree, commits, ledger CLAIM).

    Pull request evidence is not read here (no GitHub read from this host); read it with
    ticket_prs before acting on an UNSTARTED verdict.
    """
    from .linear_tools import ModelLinearIssuesInput, _issues

    p = cast(ModelTicketsInput, params)
    rows, _, _ = _issues(
        ctx,
        ModelLinearIssuesInput(
            scope="workspace", ids=list(p.tickets), include_archived=True
        ),
    )
    metadata = {
        str(r["id"]): {
            "state": r.get("state"),
            "assignee": r.get("assignee"),
            "has_children": bool(r.get("children")),
        }
        for r in rows
    }
    meta_file = (
        ctx.state()
        / f"triage-started-metadata-{len(list(ctx.state().glob('triage-started-metadata-*')))}.json"
    )
    meta_file.write_text(json.dumps(metadata))
    done = _whole(
        ctx,
        "triage_started_command",
        [*p.tickets, "--no-prs", "--metadata", str(meta_file)],
        timeout=600,
    )
    try:
        report = json.loads(done.stdout)
    except ValueError:
        return {
            "exit": done.returncode,
            "stdout_tail": _tail(done.stdout),
            "stderr": _tail(done.stderr),
        }
    keep = (
        "ticket",
        "state",
        "assignee",
        "external",
        "has_children",
        "worktree",
        "commits_ahead",
        "last_commit_ts",
        "last_claim_ts",
        "last_claim_lane",
        "fresh_signals",
        "verdict",
        "reason",
    )
    return {
        "exit": done.returncode,
        "now": report.get("now"),
        "window_hours": report.get("window_hours"),
        "rule": report.get("rule"),
        "counts": report.get("counts"),
        "errors": report.get("errors"),
        "tickets": cast(
            JsonValue,
            [{k: t.get(k) for k in keep} for t in report.get("tickets") or []],
        ),
        "stderr": _tail(done.stderr),
        "pr_evidence": "not read by this tool (no GitHub read on this host); use ticket_prs",
    }


def dod_verify(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """The DoD verifier against one ticket's contract (hosted audience), its verdict compacted."""
    p = cast(ModelTicketInput, params)
    from .local_reads import ticket_prs

    prs = ticket_prs(ctx, p)
    opened = cast(list[dict[str, JsonValue]], prs["open"])
    merged = [
        *cast(list[JsonValue], prs["merged"]),
        *cast(list[JsonValue], prs["merged_in_history"]),
    ]
    if opened:
        raise MorningToolRefusalError(
            f"{p.ticket} is not CLOSABLE: open PR {', '.join(str(o['pr']) for o in opened)}; "
            "hold it, never flip it (the verifier runs only when every PR is merged)"
        )
    if not merged:
        raise MorningToolRefusalError(
            f"{p.ticket} has no merged PR in the watcher record or the integration branches' "
            "history, so it is not CLOSABLE; hold it"
        )
    budget = ctx.state() / f"dod-verify-{ctx.phase_label}.count"
    used = int(budget.read_text()) if budget.is_file() else 0
    if used >= DOD_VERIFY_BUDGET:
        raise MorningToolRefusalError(
            f"the verifier budget ({DOD_VERIFY_BUDGET} runs per phase) is spent; hold the "
            "remaining CLOSABLE tickets with the reason 'verifier budget spent'"
        )
    budget.write_text(str(used + 1))
    done = _whole(
        ctx,
        "dod_verify_command",
        [p.ticket, "--execution-audience", "hosted"],
        timeout=900,
    )
    receipt = next(
        (line for line in reversed(done.stdout.splitlines()) if line.startswith("{")),
        "",
    )
    try:
        loaded = json.loads(receipt)
    except ValueError:
        return {
            "ticket": p.ticket,
            "exit": done.returncode,
            "verdict": "UNKNOWN: no receipt",
            "stdout_tail": _tail(done.stdout),
            "stderr": _tail(done.stderr),
        }
    result = cast(dict[str, object], loaded.get("result") or {})
    payload = cast(dict[str, object], result.get("terminal_payload") or {})
    checks = cast(list[dict[str, object]], payload.get("checks") or [])
    counts = (
        "total_checks",
        "verified_count",
        "failed_count",
        "skipped_count",
        "behavior_proving_count",
        "readback_proving_count",
    )
    return {
        "ticket": p.ticket,
        "exit": done.returncode,
        "skill_status": loaded.get("status"),
        "status": payload.get("status"),
        **{k: cast(JsonValue, payload.get(k)) for k in counts},
        "error_message": cast(JsonValue, payload.get("error_message")),
        "checks": cast(
            JsonValue,
            [
                {
                    "id": c.get("evidence_id"),
                    "status": c.get("status"),
                    "proof_class": c.get("proof_class"),
                    "binds_ac": c.get("binds_ac"),
                    "description": str(c.get("description") or "")[:200],
                    "message": str(c.get("message") or "")[:300],
                }
                for c in checks[:30]
            ],
        ),
        "correlation_id": loaded.get("correlation_id"),
    }


# --- the publishing phase: push, one PR, the handoff --------------------------------------


def kb_push_branch(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """PUBLISH ONLY: push the run's knowledge-base branch from its worktree (never forced)."""
    _publish_only(ctx, f"pushing {ctx.kb_branch}")
    _worktree_ready(ctx)
    dirty = _git_out(ctx.kb_worktree, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise MorningToolRefusalError(
            f"the worktree has uncommitted tracked changes: {dirty[:400]}"
        )
    done = _git_hooked(ctx.kb_worktree, "push", "-u", "origin", ctx.kb_branch)
    head = _git_out(ctx.kb_worktree, "rev-parse", "HEAD") or ""
    return {"branch": ctx.kb_branch, "head": head, **done}


def _pr_file(ctx: ModelMorningToolContext) -> Path:
    return ctx.state() / f"kb-pr-{ctx.date}.json"


class ModelOpenPrInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(
        min_length=80,
        max_length=20000,
        pattern=r"^[^\x00]*$",
        description="the PR body: what each phase delivered or BLOCKED, the goal summary",
    )


def kb_open_pr(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    """PUBLISH ONLY: open the morning's one knowledge-base PR for the pushed branch."""
    p = cast(ModelOpenPrInput, params)
    _publish_only(ctx, "opening the morning PR")
    _worktree_ready(ctx)
    body_file = ctx.state() / f"kb-pr-body-{ctx.date}.md"
    body_file.write_text(p.body, encoding="utf-8")
    title = f"{ctx.commit_prefix} morning ground state {ctx.date}"
    done = _proc(
        [
            *ctx.command("kb_pr_create_command"),
            "--base",
            "main",
            "--head",
            ctx.kb_branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ],
        cwd=ctx.kb_worktree,
        timeout=180,
    )
    text = f"{done['stdout']}\n{done['stderr']}"
    url = re.search(r"https://github\.com/\S+/pull/(\d+)", text)
    if not url:
        return {"opened": False, **done}
    record: dict[str, JsonValue] = {
        "url": url.group(0),
        "number": int(url.group(1)),
        "title": title,
    }
    _pr_file(ctx).write_text(json.dumps(record))
    return {
        "opened": done["exit"] == 0,
        "existing": done["exit"] != 0,
        **record,
        "next": "pr_handoff",
    }


def pr_handoff(ctx: ModelMorningToolContext, _: BaseModel) -> Result:
    """PUBLISH ONLY: hand the morning PR to the landing controller (MSG plus this lane's TERMINAL)."""
    _publish_only(ctx, "the PR handoff")
    lane, ticket = _lane(ctx)
    if not _pr_file(ctx).is_file():
        raise MorningToolRefusalError(
            "no morning PR opened in this run: kb_open_pr first"
        )
    record = json.loads(_pr_file(ctx).read_text())
    head = _git_out(ctx.kb_worktree, "rev-parse", "HEAD") or ""
    repo = Path(ctx.kb_clone).name
    found = _cells(ctx)
    if found.get("undisposed"):
        raise MorningToolRefusalError(
            "a delegated draft is not disposed: delegate_dispose it before the handoff"
        )
    cells = str(found["ledger_cells"])
    argv = [
        *ctx.command("pr_handoff_command"),
        f"{repo}#{record['number']}",
        lane,
        "--needs",
        "land",
        "--expect-head",
        head,
        "--ticket",
        ticket,
        "--delegation",
        cells,
        "--keep-worktree",
    ]
    done: Result = {}
    for attempt in range(6):
        done = _proc(argv, cwd=ctx.root, timeout=300)
        text = f"{done['stdout']}{done['stderr']}".lower()
        if done["exit"] == 0 or "watcher" not in text:
            break
        time.sleep(60)  # the PR watcher has not seen the new PR yet
        done["attempts"] = attempt + 1
    return {"pr": record["url"], "head": head, "delegation_cells": cells, **done}


EFFECT_TOOLS: tuple[MorningTool, ...] = (
    MorningTool(
        "kb_write_artifact",
        "Write one artifact this phase owns, whole, into the run's knowledge-base worktree "
        "(refused past the format contract's line cap). Then kb_scrub and kb_commit.",
        ModelArtifactWriteInput,
        "write",
        kb_write_artifact,
    ),
    MorningTool(
        "delegate_prose",
        "Draft one piece of this phase's prose through the ONEX delegation chain (GLM, then "
        "the lab), leaving a receipt. Give it the fact table and what to write. Returns the "
        "draft and its draft_id.",
        ModelDelegateInput,
        "external",
        delegate_prose,
    ),
    MorningTool(
        "delegate_dispose",
        "Record what was done with a delegated draft (used, edited, discarded, ignored).",
        ModelDisposeInput,
        "external",
        delegate_dispose,
    ),
    MorningTool(
        "delegation_cells",
        "The delegation cell this phase's receipts back: {delegated, runs, reason} for the "
        "result's delegation field, and the TERMINAL's ledger cells.",
        ModelNoInput,
        "read",
        delegation_cells,
    ),
    MorningTool(
        "ledger_append_claim",
        "Append this phase's CLAIM row (its lane, ticket, executor and cost sentence).",
        ModelClaimInput,
        "external",
        ledger_append_claim,
    ),
    MorningTool(
        "ledger_append_terminal",
        "Append this phase's TERMINAL row closing its CLAIM, with the delegation cells its "
        "receipts back and the run date; a friction given here gets its FRICTION row first.",
        ModelTerminalInput,
        "external",
        ledger_append_terminal,
    ),
    MorningTool(
        "delegation_accounting",
        "Run the delegation accounting reader once over the run window and save its output "
        "verbatim as the dated delegation-accounting companion in the worktree.",
        ModelNoInput,
        "write",
        delegation_accounting,
    ),
    MorningTool(
        "closeout_completion_row",
        "The launchd dod-closeout-sweep's completion row for section 6b, verbatim.",
        ModelNoInput,
        "read",
        closeout_completion_row,
    ),
    MorningTool(
        "closeout_scope_verify",
        "Section 6a: run the closeout verifier once on the run's closure probe pin, timed "
        "against its ceiling; returns wall seconds and the terminal status output.",
        ModelNoInput,
        "read",
        closeout_scope_verify,
    ),
    MorningTool(
        "triage_fences",
        "The fence list (fenced_tickets), derived mechanically from the ledger and the open "
        "PRs: comment on a fenced ticket if you must, never flip it.",
        ModelNoInput,
        "read",
        triage_fences,
    ),
    MorningTool(
        "triage_started",
        "The started-node verdict per ticket (STARTED / UNSTARTED / EXTERNAL / PARENT, with "
        "worktree, commits ahead and last ledger CLAIM). PR events are read with ticket_prs.",
        ModelTicketsInput,
        "read",
        triage_started,
    ),
    MorningTool(
        "dod_verify",
        "Run the DoD verifier against one CLOSABLE ticket's contract (hosted audience): it "
        "refuses a ticket with an open PR or no merged PR, and after 20 runs per phase.",
        ModelTicketInput,
        "read",
        dod_verify,
    ),
    MorningTool(
        "kb_push_branch",
        "PUBLISH ONLY: push the run's knowledge-base branch (never forced).",
        ModelNoInput,
        "external",
        kb_push_branch,
    ),
    MorningTool(
        "kb_open_pr",
        "PUBLISH ONLY: open the morning's one knowledge-base PR for the pushed branch (an "
        "existing PR for the branch is returned instead).",
        ModelOpenPrInput,
        "external",
        kb_open_pr,
    ),
    MorningTool(
        "pr_handoff",
        "PUBLISH ONLY: hand the morning PR to the landing controller. This writes the phase's "
        "TERMINAL itself; do not also call ledger_append_terminal.",
        ModelNoInput,
        "external",
        pr_handoff,
    ),
)
