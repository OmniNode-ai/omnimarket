"""Exception queue inputs and outputs (OMN-17427).

Frozen dataclasses keep the queue_status.py import usable with only the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelExceptionRow:
    cls: str  # the exception's class, a kebab slug: the kind of case code could not resolve
    key: str  # the case inside its class; the same open key reported again is the same case
    source: str  # the detector or actor that emitted it
    evidence: str  # what it read, verbatim
    tried: str  # what it did before giving up
    proposed_fix: str  # what would resolve it
    decision: bool = False  # only a decision row reaches the operator
    ticket: str = ""
    repo: str = ""


@dataclass(frozen=True)
class ModelExceptionEmitRequest:
    state_dir: Path
    row: ModelExceptionRow
    now: str
    runner: Path | None = None  # the remote-lane runner; None never dispatches
    parent: str | None = None


@dataclass(frozen=True)
class ModelExceptionEmitResult:
    status: str  # appended | repeat | refused
    id: str
    occurrence: int
    mechanize: str  # '' below two, else sent:<lane> | already:<lane> | unsent:<why>
    detail: str


@dataclass(frozen=True)
class ModelExceptionQueueRequest:
    state_dir: Path
    audience: str  # operator (decision rows) | orchestrator (every other open row)


@dataclass(frozen=True)
class ModelExceptionQueueResult:
    rows: list[dict[str, Any]]
    lines: list[str]


@dataclass(frozen=True)
class ModelExceptionResolveRequest:
    state_dir: Path
    id: str
    by: str
    how: str
    now: str


@dataclass(frozen=True)
class ModelExceptionResolveResult:
    status: str
    detail: str


@dataclass(frozen=True)
class ModelExceptionCliRequest:
    argv: list[
        str
    ]  # queue_status.py --exception's arguments: emit|list|resolve and their flags


@dataclass(frozen=True)
class ModelExceptionCliResult:
    code: int  # 0 done; 3 a refused row or an unknown id; 2 bad arguments
    out: str  # what was printed


@dataclass(frozen=True)
class ModelExceptionQueueReadRequest:
    state_dir: Path | None = (
        None  # default $ONEX_STATE_DIR/exception-queue, else $ONEX_REGISTRY_ROOT/.onex_state/exception-queue
    )


@dataclass(frozen=True)
class ModelExceptionQueueRead:
    line: str  # the queue-status block's `exceptions` line
    reasons: list[
        str
    ]  # its DEGRADED reasons: decision rows, open rows, classes seen twice with no mechanize lane
    actions: list[str]  # one action per reason
