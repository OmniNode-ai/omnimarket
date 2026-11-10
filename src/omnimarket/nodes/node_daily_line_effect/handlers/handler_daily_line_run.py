# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scheduled run of the daily line: read the three sources, compute the line, publish it, write one STATUS row.

The sources are the floor record (piece 1's reading and the J1 join row) and the rolling ledger (claims with no
TERMINAL, counted by one open-claims point query over the bus). A source that cannot be read becomes ``None`` for the
compute handler, which renders ``UNKNOWN`` for its part. The line is published as ``daily-line.json`` beside the floor record, where the
floor and the morning brief read it, and then one ledger STATUS row carries the same sentence.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_daily_line import (
    ModelDailyLinePiece,
    ModelDailyLinePublication,
    ModelDailyLineRequest,
    ModelDailyLineResult,
    ModelDailyLineRunCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
    open_claims_query,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
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
    #: The variables that hold the bus read and append commands, and the host name; each command is deployment data.
    ledger_read_command_env: str = Field(min_length=1)
    ledger_read_timeout_seconds: int = Field(ge=1, le=600)
    ledger_append_command_env: str = Field(min_length=1)
    ledger_append_timeout_seconds: int = Field(ge=1)
    host_env: str = Field(min_length=1)
    floor_record_relpath: str = Field(min_length=1)
    floor_record_max_age_seconds: int = Field(ge=1)
    published_relpath: str = Field(min_length=1)
    piece: int = Field(ge=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str = Field(min_length=1)


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


def count_open_claims(read: ProtocolLedgerPointRead | None) -> int | None:
    """The open claims the bus-fed query node counts, or ``None`` when it did not answer (never zero for a failed read)."""
    if read is None:
        return None
    answer = read.read(open_claims_query())
    return None if answer.error else answer.matched


def bus_appender(
    appender: ProtocolReadingAppender,
    *,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> LedgerAppender:
    """Append a row over the bus. Anything but an accepted or duplicate receipt raises."""

    def append(row: str) -> None:
        outcome = appender.append(row, new_id())
        if not outcome.ok:
            raise RuntimeError(
                f"the ledger append was {outcome.status}: {outcome.message[:300]}"
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
        ledger: ProtocolLedgerPointRead | None,
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
                open_claims=count_open_claims(self._ledger),
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
    compute: ProtocolDailyLineCompute, env: Mapping[str, str] | None = None
) -> HandlerDailyLineRun:
    """The production wiring: the registry home and the two bus commands from the environment, the rest from the contract."""
    environment = dict(os.environ if env is None else env)
    runtime = load_runtime()
    home = Path(environment[runtime.home_env])
    read_argv = shlex.split(environment.get(runtime.ledger_read_command_env, ""))
    append_argv = shlex.split(environment.get(runtime.ledger_append_command_env, ""))
    if not append_argv:
        raise RuntimeError(
            f"{runtime.ledger_append_command_env} is not set; the status row has no bus route"
        )
    reader = (
        CommandPointRead(read_argv, runtime.ledger_read_timeout_seconds)
        if read_argv
        else None
    )
    host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
    return HandlerDailyLineRun(
        runtime,
        home=home,
        ledger=reader,
        appender=bus_appender(
            CommandLedgerAppender(
                append_argv,
                runtime.lane,
                host,
                runtime.ledger_append_timeout_seconds,
            )
        ),
        compute=compute,
    )


def dump(publication: ModelDailyLinePublication) -> str:
    return json.dumps(publication.model_dump(mode="json"), indent=2)
