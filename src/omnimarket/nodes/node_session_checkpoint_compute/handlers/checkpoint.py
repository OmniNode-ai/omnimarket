# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fixed-format session checkpoint: write, read, validate, and pick the newest one a session owns.

A checkpoint is what the next session needs to pick its loops back up mechanically: the workflows
that were running (task id, run id, script, purpose, role, status), the open decisions, and the next
deadlines. Schema ``omni.session-checkpoint/1``:

  schema          "omni.session-checkpoint/1"                       required
  written_at      UTC stamp YYYY-MM-DDTHH:MM:SSZ                    required
  session_lane    the session lane                                  required
  principal       the ledger principal of the session that wrote it optional
  session_id      Claude Code session id                            optional
  session_ticket  OMN-n                                             optional
  workflows[]     task_id, run_id, script, purpose, role, status    required (list may be empty)
                  ends_at (UTC; required for a running drain), start_at (UTC)
                  role   in drain | drain-successor | lane | other
                  status in running | queued | stopped | done
  decisions[]     what, owner (operator | session); since (UTC)    required (list may be empty)
  deadlines[]     what, at (UTC), action                            required (list may be empty)
  ticks[]         name, cadence (cron), prompt                      optional
  notes[]         free text                                         optional

``principal`` binds a checkpoint to the session that wrote it. An operator session reads every
checkpoint, as before; a member session reads only checkpoints carrying one of its own principals,
so a newer checkpoint of another principal, or one carrying no principal, is never its newest and
nothing it names is stopped, resumed or inherited.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.models.session_scope import ModelSessionScope

SCHEMA_ID = "omni.session-checkpoint/1"
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LANE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]*$")
TICKET_RE = re.compile(r"^OMN-\d+$")
ROLES = ("drain", "drain-successor", "lane", "other")
STATUSES = ("running", "queued", "stopped", "done")
OWNERS = ("operator", "session")
SUFFIX = "-checkpoint.json"
PROSE_SUFFIX = ".md"
MAX_AGE_HOURS = 12

WORKFLOW_REQUIRED = ("task_id", "run_id", "script", "purpose", "role", "status")
DECISION_REQUIRED = ("what", "owner")
DEADLINE_REQUIRED = ("what", "at", "action")
TICK_REQUIRED = ("name", "cadence", "prompt")
TOP_REQUIRED = (
    "schema",
    "written_at",
    "session_lane",
    "workflows",
    "decisions",
    "deadlines",
)
KEY_ALIASES: dict[str, dict[str, str]] = {
    "workflow": {
        "task": "task_id",
        "run": "run_id",
        "ends": "ends_at",
        "start": "start_at",
    },
    "decision": {},
    "deadline": {},
    "tick": {},
}
ALLOWED: dict[str, set[str]] = {
    "workflow": set(WORKFLOW_REQUIRED) | {"ends_at", "start_at"},
    "decision": set(DECISION_REQUIRED) | {"since"},
    "deadline": set(DEADLINE_REQUIRED),
    "tick": set(TICK_REQUIRED),
}
# A prose handoff names the principal that wrote it on a line of its own.
_PROSE_PRINCIPAL_RE = re.compile(r"(?m)^\s*principal\s*[:=]\s*(\S+)\s*$")


class RefusedError(Exception):
    """The input does not match the schema, or the session may not use it; carries every finding."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__("; ".join(findings))
        self.findings = findings


class InputError(Exception):
    """A file input is missing or unreadable."""


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def age_hours(data: dict[str, Any], now: datetime) -> float:
    return (now - parse_stamp(data["written_at"])).total_seconds() / 3600


def parse_kv(kind: str, text: str) -> dict[str, str]:
    """Parse ``key=value;key=value`` into a record with the schema's key names."""
    record: dict[str, str] = {}
    findings: list[str] = []
    for part in text.split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            findings.append(f"{kind}: '{part.strip()}' is not key=value")
            continue
        key, value = part.split("=", 1)
        key = KEY_ALIASES[kind].get(key.strip(), key.strip())
        if key not in ALLOWED[kind]:
            findings.append(
                f"{kind}: unknown key '{key}' (allowed: {', '.join(sorted(ALLOWED[kind]))})"
            )
            continue
        record[key] = value.strip()
    if findings:
        raise RefusedError(findings)
    return record


def _check_items(
    data: dict[str, Any], field: str, required: tuple[str, ...], allowed: set[str]
) -> list[str]:
    items = data.get(field)
    if items is None:
        return []
    if not isinstance(items, list):
        return [f"{field} must be a list"]
    findings: list[str] = []
    for index, item in enumerate(items):
        where = f"{field}[{index}]"
        if not isinstance(item, dict):
            findings.append(f"{where} must be an object")
            continue
        for key in required:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(f"{where} is missing {key}")
        for key in item:
            if key not in allowed:
                findings.append(f"{where} has unknown key {key}")
    return findings


def validate(data: Any) -> list[str]:
    """Every schema finding; an empty list means the checkpoint is valid."""
    if not isinstance(data, dict):
        return ["checkpoint must be a JSON object"]
    findings = [f"missing {key}" for key in TOP_REQUIRED if key not in data]
    if data.get("schema", SCHEMA_ID) != SCHEMA_ID:
        findings.append(f"schema must be {SCHEMA_ID}")
    written = data.get("written_at")
    if written is not None and (
        not isinstance(written, str) or not STAMP_RE.match(written)
    ):
        findings.append("written_at must be a UTC stamp YYYY-MM-DDTHH:MM:SSZ")
    lane = data.get("session_lane")
    if lane is not None and (not isinstance(lane, str) or not LANE_RE.match(lane)):
        findings.append("session_lane must be one lane token")
    if "principal" in data and not (
        isinstance(data["principal"], str) and PRINCIPAL_RE.match(data["principal"])
    ):
        findings.append("principal must be one principal token")
    ticket = data.get("session_ticket")
    if ticket is not None and (
        not isinstance(ticket, str) or not TICKET_RE.match(ticket)
    ):
        findings.append("session_ticket must be OMN-n")
    if "session_id" in data and not isinstance(data["session_id"], str):
        findings.append("session_id must be a string")
    known = set(TOP_REQUIRED) | {
        "session_ticket",
        "session_id",
        "principal",
        "ticks",
        "notes",
    }
    findings += [f"unknown top-level key {key}" for key in data if key not in known]
    findings += _check_items(data, "workflows", WORKFLOW_REQUIRED, ALLOWED["workflow"])
    findings += _check_items(data, "decisions", DECISION_REQUIRED, ALLOWED["decision"])
    findings += _check_items(data, "deadlines", DEADLINE_REQUIRED, ALLOWED["deadline"])
    findings += _check_items(data, "ticks", TICK_REQUIRED, ALLOWED["tick"])
    for index, wf in enumerate(data.get("workflows") or []):
        if not isinstance(wf, dict):
            continue
        where = f"workflows[{index}]"
        if wf.get("role") and wf["role"] not in ROLES:
            findings.append(f"{where}.role must be one of {', '.join(ROLES)}")
        if wf.get("status") and wf["status"] not in STATUSES:
            findings.append(f"{where}.status must be one of {', '.join(STATUSES)}")
        for key in ("ends_at", "start_at"):
            if key in wf and not (isinstance(wf[key], str) and STAMP_RE.match(wf[key])):
                findings.append(f"{where}.{key} must be a UTC stamp")
        if (
            wf.get("role") == "drain"
            and wf.get("status") == "running"
            and "ends_at" not in wf
        ):
            findings.append(
                f"{where} is a running drain with no ends_at (its time box)"
            )
    for index, dec in enumerate(data.get("decisions") or []):
        if isinstance(dec, dict) and dec.get("owner") and dec["owner"] not in OWNERS:
            findings.append(
                f"decisions[{index}].owner must be one of {', '.join(OWNERS)}"
            )
        if (
            isinstance(dec, dict)
            and "since" in dec
            and not STAMP_RE.match(str(dec["since"]))
        ):
            findings.append(f"decisions[{index}].since must be a UTC stamp")
    for index, dl in enumerate(data.get("deadlines") or []):
        if isinstance(dl, dict) and dl.get("at") and not STAMP_RE.match(str(dl["at"])):
            findings.append(f"deadlines[{index}].at must be a UTC stamp")
    if "notes" in data and not (
        isinstance(data["notes"], list)
        and all(isinstance(x, str) for x in data["notes"])
    ):
        findings.append("notes must be a list of strings")
    return findings


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read checkpoint {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RefusedError([f"{path} is not JSON: {exc}"]) from exc
    findings = validate(data)
    if findings:
        raise RefusedError(findings)
    checked: dict[str, Any] = data
    return checked


def owned(scope: ModelSessionScope, data: dict[str, Any]) -> bool:
    """Whether this session may read, resume from or inherit from the checkpoint."""
    principal = data.get("principal")
    return scope.owns(principal if isinstance(principal, str) else None)


def records(directory: Path, scope: ModelSessionScope) -> list[dict[str, Any]]:
    """The valid checkpoints this session owns, newest first."""
    result: list[dict[str, Any]] = []
    if not directory.is_dir():
        return result
    for path in directory.glob(f"*{SUFFIX}"):
        try:
            data = load(path)
            parse_stamp(data["written_at"])
        except (InputError, RefusedError, ValueError, TypeError):
            continue
        if owned(scope, data):
            result.append(data)
    return sorted(result, key=lambda data: str(data["written_at"]), reverse=True)


def newest(directory: Path, scope: ModelSessionScope) -> Path | None:
    """The valid checkpoint this session owns with the newest written_at, or None."""
    best: tuple[str, Path] | None = None
    if not directory.is_dir():
        return None
    for path in directory.glob(f"*{SUFFIX}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if validate(data) or not owned(scope, data):
            continue
        if best is None or data["written_at"] > best[0]:
            best = (data["written_at"], path)
    return best[1] if best else None


def newest_prose_handoff(directory: Path, scope: ModelSessionScope) -> Path | None:
    """The newest prose handoff this session owns (the fallback when no checkpoint is found).

    An operator session takes the newest one, as before. A member session takes only a handoff
    that names one of its own principals on a ``principal:`` line; an unattributed one is not its.
    """
    if not directory.is_dir():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for path in directory.glob(f"*{PROSE_SUFFIX}"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            modified = path.stat().st_mtime
        except OSError:
            continue
        if not scope.is_operator:
            named = set(_PROSE_PRINCIPAL_RE.findall(text))
            if not named or not all(scope.owns(p) for p in named):
                continue
        candidates.append((modified, path.name, path))
    return max(candidates)[2] if candidates else None


def build(
    *,
    lane: str,
    now: str,
    scope: ModelSessionScope,
    base: dict[str, Any] | None,
    ticket: str | None = None,
    session_id: str | None = None,
    workflows: tuple[str, ...] = (),
    decisions: tuple[str, ...] = (),
    deadlines: tuple[str, ...] = (),
    ticks: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    base = base or {}
    if not isinstance(base, dict):
        raise RefusedError(["the starting checkpoint must hold a JSON object"])
    if (
        not scope.is_operator
        and ("principal" in base or "schema" in base)
        and not owned(scope, base)
    ):
        raise RefusedError(
            [
                "member session: the starting checkpoint was written by another principal "
                "(or names none); a member builds only on its own checkpoints"
            ]
        )
    data: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "written_at": now,
        "session_lane": lane,
        "workflows": list(base.get("workflows") or []),
        "decisions": list(base.get("decisions") or []),
        "deadlines": list(base.get("deadlines") or []),
        "ticks": list(base.get("ticks") or []),
        "notes": list(base.get("notes") or []),
    }
    if scope.writer_principal is not None:
        data["principal"] = scope.writer_principal
    sid = session_id if session_id is not None else base.get("session_id")
    if sid is not None:
        data["session_id"] = sid
    tkt = ticket or base.get("session_ticket")
    if tkt:
        data["session_ticket"] = tkt
    findings: list[str] = []
    for kind, field, values in (
        ("workflow", "workflows", workflows),
        ("decision", "decisions", decisions),
        ("deadline", "deadlines", deadlines),
        ("tick", "ticks", ticks),
    ):
        for text in values:
            try:
                data[field].append(parse_kv(kind, text))
            except RefusedError as exc:
                findings += exc.findings
    data["notes"] += list(notes)
    # A flag-given workflow replaces a carried one with the same task id: the newest state wins.
    seen: dict[str, int] = {}
    merged: list[Any] = []
    for wf in data["workflows"]:
        tid = wf.get("task_id") if isinstance(wf, dict) else None
        if tid and tid in seen:
            merged[seen[tid]] = wf
            continue
        if tid:
            seen[tid] = len(merged)
        merged.append(wf)
    data["workflows"] = merged
    findings += validate(data)
    if findings:
        raise RefusedError(findings)
    return data


def write(data: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = data["written_at"].replace(":", "").replace("T", "-")
    path = directory / f"{name}-{data['session_lane']}{SUFFIX}"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def summary(data: dict[str, Any]) -> str:
    wfs = data["workflows"]
    running = sum(1 for w in wfs if w.get("status") == "running")
    queued = sum(1 for w in wfs if w.get("status") == "queued")
    return (
        f"workflows={len(wfs)} running={running} queued={queued} "
        f"decisions={len(data['decisions'])} deadlines={len(data['deadlines'])} "
        f"ticks={len(data.get('ticks') or [])}"
    )


def read_lines(data: dict[str, Any], path: Path) -> list[str]:
    """One mechanical line per item, in the order the next session acts on them."""
    principal = data.get("principal") or "-"
    lines = [
        f"CHECKPOINT path={path} written_at={data['written_at']} lane={data['session_lane']} "
        f"principal={principal} " + summary(data)
    ]
    for wf in data["workflows"]:
        verb = {"running": "RESUME", "queued": "QUEUED"}.get(wf["status"])
        if not verb:
            continue
        extra = "".join(f" {k}={wf[k]}" for k in ("ends_at", "start_at") if k in wf)
        lines.append(
            f"{verb} task={wf['task_id']} run={wf['run_id']} role={wf['role']}{extra} "
            f"script={wf['script']} purpose={wf['purpose']}"
        )
    for dl in sorted(data["deadlines"], key=lambda d: d["at"]):
        lines.append(f"DEADLINE at={dl['at']} what={dl['what']} action={dl['action']}")
    for dec in data["decisions"]:
        since = f" since={dec['since']}" if "since" in dec else ""
        lines.append(f"DECISION owner={dec['owner']}{since} what={dec['what']}")
    for tick in data.get("ticks") or []:
        lines.append(
            f"TICK name={tick['name']} cadence={tick['cadence']} prompt={tick['prompt']}"
        )
    return lines
