# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scheduled run of the daily line: read the three sources, compute the line, publish it, write one STATUS row.

The sources are the floor record (piece 1's reading and the J1 join row) and the rolling ledger (claims with no
TERMINAL, counted by the claim index replay). A source that cannot be read becomes ``None`` for the compute handler,
which renders ``UNKNOWN`` for its part. The line is published as ``daily-line.json`` beside the floor record, where the
floor and the morning brief read it, and then one ledger STATUS row carries the same sentence.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.claim_index import build_index_from_sources, window_sources
from omnimarket.models.model_daily_line import (
    ModelDailyLinePiece,
    ModelDailyLinePublication,
    ModelDailyLineRequest,
    ModelDailyLineResult,
    ModelDailyLineRunCommand,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
NODE_NAME = "node_daily_line_effect"
J1_JOIN_ID = "J1"

LedgerAppender = Callable[[str], None]


class ProtocolDailyLineCompute(Protocol):
    """The computation of the line, injected by the composition root (it is another node's handler)."""

    def handle(self, request: ModelDailyLineRequest) -> ModelDailyLineResult: ...


Clock = Callable[[], datetime]


class ModelDailyLineRuntime(BaseModel):
    """The contract's ``runtime`` block: where the sources are, where the line goes and how a ledger row is appended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    ledger_env: str = Field(min_length=1)
    ledger_relpath: str = Field(min_length=1)
    floor_record_relpath: str = Field(min_length=1)
    floor_record_max_age_seconds: int = Field(ge=1)
    published_relpath: str = Field(min_length=1)
    piece: int = Field(ge=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str = Field(min_length=1)
    #: The append command; ``{ledger}`` and ``{row}`` are filled in, one argument each, never through a shell.
    append_command: tuple[str, ...] = Field(min_length=1)


def load_runtime(path: Path = CONTRACT_PATH) -> ModelDailyLineRuntime:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelDailyLineRuntime.model_validate(contract["runtime"])


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def read_floor_record(
    path: Path, *, piece: int, now: datetime, max_age_seconds: int
) -> tuple[ModelDailyLinePiece | None, str | None]:
    """Piece ``piece``'s reading and the J1 join state from the floor record; ``None`` for what is not readable.

    A record that is missing, is not JSON, has no ``pieces`` list, carries no ``generated_at`` or is older than
    ``max_age_seconds`` reads as nothing: a stale floor is not a reading.
    """
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(document, dict) or not isinstance(document.get("pieces"), list):
        return None, None
    generated = _parse_time(document.get("generated_at"))
    if generated is None or (now - generated).total_seconds() > max_age_seconds:
        return None, None
    rows = [row for row in document["pieces"] if isinstance(row, dict)]
    j1_state: str | None = None
    for row in rows:
        for join in row.get("joins") or []:
            if (
                isinstance(join, dict)
                and join.get("id") == J1_JOIN_ID
                and isinstance(join.get("state"), str)
            ):
                j1_state = join["state"]
    own = next((row for row in rows if row.get("piece") == piece), None)
    if own is None:
        return None, j1_state
    state = own.get("reading_state")
    text = own.get("text")
    return (
        ModelDailyLinePiece(
            state=state if isinstance(state, str) else "",
            detail=text if isinstance(text, str) else "",
        ),
        j1_state,
    )


def count_open_claims(ledger: Path, *, now: datetime) -> int | None:
    """The tickets the claim index holds with no TERMINAL or RELEASE, or ``None`` when the ledger cannot be replayed."""
    try:
        sources = window_sources(ledger, ledger.name, now=now)
        index = build_index_from_sources(sources, now=now)
    except (OSError, RuntimeError, ValueError):
        return None
    return len(index["tickets"])


def command_appender(
    command: Sequence[str],
    ledger: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LedgerAppender:
    """Append a row by running the configured command, with no shell. A non-zero exit raises."""

    def append(row: str) -> None:
        argv = [
            part.replace("{ledger}", str(ledger)).replace("{row}", row)
            for part in command
        ]
        done = run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(
                f"the ledger append exited {done.returncode}: {done.stderr.strip()[:300]}"
            )

    return append


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # One temporary file per call: overlapping runs never share a name, so neither can take the other's.
    handle, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def status_row(
    runtime: ModelDailyLineRuntime, publication: ModelDailyLinePublication
) -> str:
    stamp = publication.generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    unknown = ",".join(publication.unknown_parts) or "none"
    return (
        f"{stamp} | STATUS | lane={runtime.lane} | ticket={runtime.ticket} | "
        f"actor=script:{NODE_NAME} | model=none | unknown={unknown} | {publication.line}"
    )


class HandlerDailyLineRun:
    """Run the daily line once: read, compute, publish to the floor record's directory, write one STATUS row."""

    def __init__(
        self,
        runtime: ModelDailyLineRuntime,
        *,
        home: Path,
        ledger: Path,
        appender: LedgerAppender,
        clock: Clock = lambda: datetime.now(UTC),
        compute: ProtocolDailyLineCompute,
    ) -> None:
        self._runtime = runtime
        self._home = home
        self._ledger = ledger
        self._append = appender
        self._clock = clock
        self._compute = compute

    @property
    def published_path(self) -> Path:
        return self._home / self._runtime.published_relpath

    def handle(self, request: ModelDailyLineRunCommand) -> ModelDailyLinePublication:
        return self.run()

    def run(self) -> ModelDailyLinePublication:
        now = self._clock()
        piece1, j1_state = read_floor_record(
            self._home / self._runtime.floor_record_relpath,
            piece=self._runtime.piece,
            now=now,
            max_age_seconds=self._runtime.floor_record_max_age_seconds,
        )
        result = self._compute.handle(
            ModelDailyLineRequest(
                piece1=piece1,
                j1_state=j1_state,
                open_claims=count_open_claims(self._ledger, now=now),
            )
        )
        publication = ModelDailyLinePublication(
            generated_at=now,
            line=result.line,
            parts=result.parts,
            unknown_parts=result.unknown_parts,
        )
        write_atomic(self.published_path, publication.model_dump_json(indent=2) + "\n")
        self._append(status_row(self._runtime, publication))
        return publication


def build_runner(
    compute: ProtocolDailyLineCompute, env: dict[str, str] | None = None
) -> HandlerDailyLineRun:
    """The production wiring: the registry home and the ledger from the environment, the rest from the contract."""
    environment = dict(os.environ if env is None else env)
    runtime = load_runtime()
    home = Path(environment[runtime.home_env])
    ledger = Path(environment.get(runtime.ledger_env) or home / runtime.ledger_relpath)
    return HandlerDailyLineRun(
        runtime,
        home=home,
        ledger=ledger,
        appender=command_appender(runtime.append_command, ledger),
        compute=compute,
    )


def dump(publication: ModelDailyLinePublication) -> str:
    return json.dumps(publication.model_dump(mode="json"), indent=2)
