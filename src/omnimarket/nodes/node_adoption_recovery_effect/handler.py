"""Adoption journal reads, detached recovery and durable tick state (OMN-19998)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ModelAdoptionRecoveryRequest


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def write_json_atomic(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{os.getpid()}")
    scratch.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(scratch, path)


ADOPT_STALE_MIN = 60  # same grace as loops_check.PROCESS_FIX_GRACE_MIN
ADOPT_START_MARK_FILE = "throughput-tick.adopt-start.json"
ADOPT_LOG_FILE = "throughput-tick.adopt-run.log"
ADOPT_ON_TICK_ENV = "ONEX_ADOPT_ON_TICK"
ADOPT_STATE_ENV = "ONEX_ADOPTION_STATE"
ADOPT_FIX = (
    "launchd job ai.omninode.adopt-merged runs adopt_merged.py on its interval; read "
    "`launchctl list ai.omninode.adopt-merged` and `adopt_merged.py status`, and the tick's run output "
    "in throughput-tick.adopt-run.log in the session-start state directory"
)


def last_adoption(events: Path) -> datetime | None:
    """The newest timestamp in the adoption journal's last 65536 bytes, or None when it cannot be read."""
    try:
        with events.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read(65536)
    except OSError:
        return None
    newest = None
    for line in tail.splitlines():
        try:
            row = json.loads(line)
            stamp = parse_time(row.get("ts")) if isinstance(row, dict) else None
        except (ValueError, UnicodeError, OverflowError, OSError):
            continue
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    return newest


def adoption_findings(
    now: datetime, events: Path, mark: Path, start: Callable[[], str | None]
) -> list[tuple[str, str, str, str]]:
    """Report stopped adoption and start one detached run per grace window until its journal advances."""
    last = last_adoption(events)
    if last is not None:
        age = (now - last).total_seconds() / 60
        if age <= ADOPT_STALE_MIN:
            return []
        seen = (
            f"newest adopt_merged.py run event {last:%Y-%m-%dT%H:%M:%SZ} is {age:.0f}m old "
            f"(bound {ADOPT_STALE_MIN}m)"
        )
    else:
        seen = f"no adopt_merged.py run event in {events}"
    try:
        doc = json.loads(mark.read_text(encoding="utf-8"))
        started_text = doc.get("started") if isinstance(doc, dict) else None
        started = parse_time(started_text)
    except (OSError, ValueError, UnicodeError, OverflowError):
        started = None
    if started is not None and (now - started).total_seconds() / 60 <= ADOPT_STALE_MIN:
        return [
            (
                "missing",
                "adoption",
                (
                    f"{seen}; the run this tick started at {started_text} "
                    "has written no event yet; a merged fix does not reach the copy that runs"
                ),
                ADOPT_FIX,
            )
        ]
    error = start()
    if error:
        return [
            (
                "missing",
                "adoption",
                (
                    f"{seen}; could not start adopt_merged.py run: {error}; "
                    "a merged fix does not reach the copy that runs"
                ),
                ADOPT_FIX,
            )
        ]
    with contextlib.suppress(OSError):
        write_json_atomic(mark, {"started": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return [
        (
            "missing",
            "adoption",
            (
                f"{seen}: a merged fix to the tick, the controller or the hooks does not reach "
                "the copy that runs; this tick started one adopt_merged.py run "
                f"(output {mark.parent / ADOPT_LOG_FILE})"
            ),
            ADOPT_FIX,
        )
    ]


def start_adoption(log: Path, script: Path) -> str | None:
    """Start adopt_merged.py detached, returning why it could not start, if anything."""
    if not script.is_file():
        return f"no {script}"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as out:
            subprocess.Popen(
                [sys.executable, "-B", str(script), "run"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        return " ".join(str(exc).split())[:120]
    return None


def fold_findings(
    result: dict[str, Any], found: list[tuple[str, str, str, str]], report_type: Any
) -> dict[str, Any]:
    """Fold (level, loop, why, fix) findings into the tick's result: a MISSING or UNKNOWN changes the status line."""
    if not found:
        return result
    rep = report_type(
        lines=list(result["lines"]),
        missing=list(result.get("missing") or []),
        unknown=list(result.get("unknown") or []),
        checked=list(result.get("checked") or []),
    )
    for level, loop, why, fix in found:
        if level in ("missing", "unknown"):
            (rep.miss if level == "missing" else rep.unk)(loop, why, fix)
        else:
            rep.lines.append(
                f"NOTE {why}" if level == "note" else f"SKIP {loop} | {why}"
            )
    if not rep.missing and not rep.unknown:
        return {**result, "lines": rep.lines}
    return {
        "lines": rep.lines,
        "status_line": rep.status("THROUGHPUT", "STALL"),
        "exit_code": 1,
        "missing": rep.missing,
        "unknown": rep.unknown,
        "checked": rep.checked,
    }


class HandlerAdoptionRecovery:
    """Read adoption freshness and launch its recovery effect when due."""

    def __init__(self, start: Callable[[Path], str | None]) -> None:
        self.start = start

    def handle(
        self, request: ModelAdoptionRecoveryRequest
    ) -> list[tuple[str, str, str, str]]:
        env = request.env
        if env.get(ADOPT_ON_TICK_ENV, "1").strip() == "0":
            return []
        state = env.get(ADOPT_STATE_ENV) or (
            str(
                Path(env["ONEX_REGISTRY_ROOT"])
                / ".claude_scratch"
                / "adoption"
                / "state.json"
            )
            if env.get("ONEX_REGISTRY_ROOT", "").strip()
            else None
        )
        if state is None:
            return [
                (
                    "unknown",
                    "adoption",
                    "no adoption state to read: set ONEX_REGISTRY_ROOT or ONEX_ADOPTION_STATE",
                    ADOPT_FIX,
                )
            ]
        base = request.session_state_dir
        return adoption_findings(
            request.now,
            Path(state).parent / "events.jsonl",
            base / ADOPT_START_MARK_FILE,
            lambda: self.start(base / ADOPT_LOG_FILE),
        )
