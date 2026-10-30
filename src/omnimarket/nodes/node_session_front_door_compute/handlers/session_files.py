# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session files and session rows, as the way-of-working session script had them.

Nothing here opens or appends to the rolling ledger. The rows are built and returned for the caller
to append; the files are the session's own scratch files under the sessions root: ``session.env``
and ``lanes.txt``. ``session.env`` now records the session's profile and, when the session is bound
to a declared principal, ``SESSION_PRINCIPAL``, which the session checkpoint hook reads to decide
which sessions a member may claim.

``lanes.txt``: one lane per line, ``<lane> <state> [<ticket>]``, ``#`` comments allowed; the states are
``dispatched``, ``handed-off`` and ``terminal``.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.session_scope import ModelSessionScope

from ..models.model_session_front_door import (
    HOST_MODES,
    LANE_STATES,
    ModelSessionRowSpec,
)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")
TICKET_RE = re.compile(r"^OMN-\d+$")
PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
PLACEHOLDERS = frozenset(
    {"", "-", "--", ".", "?", "n/a", "na", "nil", "none", "nothing", "tbd", "unknown"}
)
CATCHUP_OPEN_HEADER = re.compile(r"^1\. Open CLAIMs in the window")
CATCHUP_NEXT_HEADER = re.compile(r"^[2-9]\. ")
CATCHUP_OPEN_ROW = re.compile(r"^\s+\d+: (\S+)\s*$")
CATCHUP_TRUNCATED = re.compile(r"^\s+\.\.\. \d+ older rows in this section not shown")
LANES_HEADER = "# <lane> <state: dispatched|handed-off|terminal> [<ticket>]\n"


class RefusedError(Exception):
    """Bad input or a refused write; ``code`` is the entry's exit code (2, or 3 for taken)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def sessions_root(explicit: str | None, env: Mapping[str, str]) -> Path:
    if explicit:
        return Path(explicit)
    home = env.get("OMNI_HOME")
    if not home:
        raise RefusedError(
            "OMNI_HOME is not set (no default: set it to the registry root)"
        )
    return Path(home) / ".claude_scratch" / "sessions"


def cell(name: str, value: str) -> str:
    """A value that goes into one pipe cell: no pipe, no newline."""
    if "|" in value or "\n" in value:
        raise RefusedError(f"{name} may not contain '|' or a newline: {value!r}")
    return value.strip()


def token(name: str, value: str | None) -> str:
    if not TOKEN_RE.match(value or ""):
        raise RefusedError(
            f"{name} must be one token ([A-Za-z0-9._:/#-]), got {value!r}"
        )
    return value or ""


def stamp(name: str, value: str | None) -> str:
    if not TS_RE.match(value or ""):
        raise RefusedError(
            f"{name} must be a UTC stamp like 2026-09-24T15:00:00Z, got {value!r}"
        )
    return value or ""


def ticket(value: str | None) -> str:
    if not TICKET_RE.match(value or ""):
        raise RefusedError(f"ticket must be OMN-<digits>, got {value!r}")
    return value or ""


def not_placeholder(name: str, value: str | None) -> str:
    if (value or "").strip().lower() in PLACEHOLDERS:
        raise RefusedError(f"{name} records nothing ({value!r}); name the real value")
    return value or ""


def kv_cells(pairs: tuple[str, ...]) -> list[str]:
    out = []
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep or not KEY_RE.match(key):
            raise RefusedError(f"kv takes key=value, got {pair!r}")
        out.append(f"{key}={cell(key, val)}")
    return out


# --------------------------------------------------------------------------- rows


def claim_row(row: ModelSessionRowSpec) -> str:
    lane = token("lane", row.lane)
    tk = ticket(row.ticket)
    actor = token("actor", not_placeholder("actor", row.actor))
    if row.est_hours is None or row.est_hours <= 0:
        raise RefusedError("est_hours must be above zero")
    displaces = not_placeholder("displaces", cell("displaces", row.displaces or ""))
    cells = [stamp("ts", row.ts), "CLAIM", f"lane={lane}", f"actor={actor}"]
    if row.model:
        cells.append(f"model={token('model', row.model)}")
    cells.append(f"ticket={tk}")
    if row.scope:
        cells.append(f"scope={cell('scope', row.scope)}")
    kv = kv_cells(row.kv)
    # A session lane owns no worktree of its own; the ledger wants the cell said.
    if not any(c.startswith("worktree=") for c in kv):
        kv.append("worktree=none")
    cells += kv
    cells.append(f"est ~{row.est_hours:g} lane-hours; displaces {displaces}; ({tk})")
    cells.append(cell("text", row.text))
    return " | ".join(cells)


def status_row(row: ModelSessionRowSpec) -> str:
    cells = [stamp("ts", row.ts), "STATUS", f"lane={token('lane', row.lane)}"]
    if row.ticket:
        cells.append(f"ticket={ticket(row.ticket)}")
    if row.t0:
        cells.append(f"T0={stamp('t0', row.t0)}")
    cells += kv_cells(row.kv)
    cells.append(cell("text", row.text))
    return " | ".join(cells)


def terminal_row(row: ModelSessionRowSpec) -> str:
    friction = cell("friction", row.friction or "")
    if not friction:
        raise RefusedError(
            "friction is required: 'none', or one line naming this lane's FRICTION rows"
        )
    cells = [stamp("ts", row.ts), "TERMINAL", f"lane={token('lane', row.lane)}"]
    if row.ticket:
        cells.append(f"ticket={ticket(row.ticket)}")
    if row.closes_claim:
        cells.append(f"closes-CLAIM={stamp('closes_claim', row.closes_claim)}")
    if row.outcome:
        cells.append(f"outcome={token('outcome', row.outcome)}")
    cells.append(f"friction={friction}")
    cells += kv_cells(row.kv)
    cells.append(cell("text", row.text))
    return " | ".join(cells)


# --------------------------------------------------------------------------- files


def lane_name(purpose: str | None) -> str:
    if not PURPOSE_RE.match(purpose or ""):
        raise RefusedError(
            f"purpose must be lowercase words joined by '-', got {purpose!r}"
        )
    return f"{purpose}-{secrets.token_hex(2)}"


def session_env_body(
    *,
    lane: str,
    claim_ts: str,
    goal_ticket: str,
    host_mode: str,
    scope: ModelSessionScope,
    env: Mapping[str, str],
) -> str:
    if host_mode not in HOST_MODES:
        raise RefusedError(
            f"host mode must be one of {', '.join(HOST_MODES)}, got {host_mode!r}"
        )
    values = {
        "SESSION_LANE": token("lane", lane),
        "CLAIM_TS": stamp("claim_ts", claim_ts),
        "GOAL_TICKET": ticket(goal_ticket),
        "HOST_MODE": host_mode,
        "SESSION_PROFILE": "operator" if scope.is_operator else "team",
    }
    if scope.writer_principal:
        values["SESSION_PRINCIPAL"] = token("principal", scope.writer_principal)
    # The registry root as it stood at init: a later block sources session.env to get it back,
    # because the shell profile of each tool call can reset OMNI_HOME.
    if env.get("OMNI_HOME"):
        values["OMNI_HOME"] = cell("OMNI_HOME", str(Path(env["OMNI_HOME"]).resolve()))
    return "".join(f"{k}={v}\n" for k, v in values.items())


def init(
    root: Path, lane: str, body: str, *, dry_run: bool
) -> tuple[list[str], Path | None]:
    """Write ``session.env`` and an empty ``lanes.txt``; refuse (code 3) when it exists."""
    folder = root / token("lane", lane)
    if dry_run:
        return (
            [
                f"DRY-RUN would write {folder / 'session.env'}:",
                *body.splitlines(),
                f"DRY-RUN would write {folder / 'lanes.txt'} (header only)",
            ],
            None,
        )
    path = folder / "session.env"
    if path.exists():
        raise RefusedError(f"{path} exists; pick a new session lane name", code=3)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if not (folder / "lanes.txt").exists():
        (folder / "lanes.txt").write_text(LANES_HEADER, encoding="utf-8")
    resolved = path.resolve()
    return [f"wrote {folder / 'lanes.txt'}", f"SESSION_ENV={resolved}"], resolved


def show(root: Path, lane: str) -> list[str]:
    path = root / token("lane", lane) / "session.env"
    if not path.is_file():
        raise RefusedError(f"{path} is missing; run the session start first")
    return path.read_text(encoding="utf-8").splitlines()


def read_lanes(folder: Path) -> list[tuple[str, str]]:
    path = folder / "lanes.txt"
    if not path.is_file():
        raise RefusedError(f"{path} is missing; run init first")
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        state = parts[1] if len(parts) > 1 else "dispatched"
        if state not in LANE_STATES:
            raise RefusedError(f"{path}: unknown state {state!r} for lane {parts[0]!r}")
        rows.append((parts[0], state))
    return rows


def lanes(root: Path, lane: str, state: str | None) -> list[str]:
    return [
        name
        for name, st in read_lanes(root / token("lane", lane))
        if state is None or state == st
    ]


def boundary(root: Path, lane: str, digest: str) -> list[str]:
    """OPEN or NOT-OPEN per lane of ``lanes.txt`` from the catchup digest's open-CLAIM section."""
    rows = read_lanes(root / token("lane", lane))
    open_lanes: set[str] = set()
    in_section = seen_header = False
    for line in digest.splitlines():
        if CATCHUP_OPEN_HEADER.match(line):
            in_section = seen_header = True
            continue
        if in_section and CATCHUP_NEXT_HEADER.match(line):
            in_section = False
        if not in_section:
            continue
        if CATCHUP_TRUNCATED.match(line):
            raise RefusedError(
                "catchup truncated the open-CLAIM list; rerun it with a larger cap"
            )
        match = CATCHUP_OPEN_ROW.match(line)
        if match:
            open_lanes.add(match.group(1))
    if not seen_header:
        raise RefusedError(
            "the digest carries no catchup section 1; pass the ledger-catchup digest"
        )
    out = [
        f"{'OPEN' if name in open_lanes else 'NOT-OPEN'} {name} (lanes.txt state={state})"
        for name, state in rows
    ]
    if lane in open_lanes:
        out.append(
            f"OPEN {lane} (the session CLAIM itself; closed by the session TERMINAL)"
        )
    return out
