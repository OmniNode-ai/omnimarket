# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate, deduplicate and append ledger rows; no bus code (OMN-20275)."""

import hashlib
import re
import socket
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Literal

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.relay_admission import (
    CoreRelayAdmission,
    load_relay_requires_rule,
    relay_admission_refusals,
    relay_rows,
)
from omnimarket.models.model_relay_admission import ModelRelayRequiresRule
from omnimarket.models.work_ledger_append import (
    EnumLedgerAuthority,
    EnumWorkLedgerAppendStatus,
    ModelAppendIfAbsentPolicy,
    ModelFencedFlipPolicy,
    ModelFileJudgeRestartState,
    ModelLedgerFlipResult,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
    ModelWorkLedgerJudgeAcceptance,
    ModelWorkLedgerTerminalRefused,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.work_ledger_flip_fence import (
    FenceClosedError,
    FlipRefusedError,
    LedgerFlipFence,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    InMemoryLedgerAppendLock,
    LedgerLockTimeoutError,
    ProtocolLedgerAppendLock,
    ProtocolLedgerAppendRunner,
    ProtocolLedgerReader,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.ledger_authority import (
    ProtocolFileJudgeRestart,
    ProtocolLedgerAuthority,
)
from omnimarket.nodes.node_work_ledger_append_effect.writer_principals import (
    WriterPrincipals,
)
from omnimarket.protocols.protocol_relay_admission import ProtocolRelayAdmission
from omnimarket.protocols.protocol_work_ledger_judge_acceptance import (
    ProtocolJudgeAcceptanceRecorder,
    ProtocolJudgeSequenceSource,
)

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ([^|\r\n]+) \| ")
VIA_BUS_PREFIX = "via=bus:"
_VIA_BUS_CELL = re.compile(r"(?:^| \| )via=bus:")
_REQUESTING_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BUS_ROW_TYPES = frozenset(
    {
        "CLAIM",
        "STATUS",
        "TERMINAL",
        "HOLD",
        "RELEASE",
        "MSG",
        "ACK",
        "FRICTION",
        "CORRECTION",
        "RULING",
        "OPERATOR-CONSENT",
    }
)
_AUTHORITY_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})


_SUBJECT_KEYS = ("lane=", "from=")


def _principal_stamped(line: str, principal: str, *, after_type: bool = False) -> str:
    """``line`` with ``principal=<principal>`` as a header cell (OMN-19513).

    The cell goes right after the row's subject cell (``lane=`` or ``from=``)
    when that is the first cell after the type, so ``<ts> | CLAIM | lane=x |``
    keeps the shape every ledger reader written before bus attribution greps
    for; otherwise it goes right after the type. The caller's text,
    continuations and line endings are preserved. ``after_type`` gives the
    shape rows had before OMN-19513, for recognising a replayed request."""
    cells = line.split(" | ", 3)
    if (
        not after_type
        and len(cells) == 4
        and cells[2].strip().startswith(_SUBJECT_KEYS)
    ):
        return (
            f"{cells[0]} | {cells[1]} | {cells[2]} | principal={principal} | {cells[3]}"
        )
    timestamp, row_type, rest = line.split(" | ", 2)
    return f"{timestamp} | {row_type} | principal={principal} | {rest}"


def _via_stamped(text: str, cell: str, via_cell: str) -> str:
    """``text`` with ``via=bus:<host>`` stamped after the ``req=`` cell (OMN-24205).

    For each line matching ``_ROW_START`` that has no whole pipe cell starting
    with ``via=bus:``, insert `` | <via_cell>`` immediately after the pipe cell
    ``cell`` (that is ``req=<id>``). A line that already has a ``via=bus:`` cell
    is returned unchanged. Continuation lines are unchanged.
    """
    request_cell = re.compile(r" \| " + re.escape(cell) + r"(?= \||[\r\n]|$)")
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if not _ROW_START.match(line) or _VIA_BUS_CELL.search(line):
            out.append(line)
            continue
        out.append(request_cell.sub(lambda m: f"{m.group(0)} | {via_cell}", line, 1))
    return "".join(out)


class HandlerWorkLedgerAppendEffect:
    def __init__(
        self,
        runner: ProtocolLedgerAppendRunner | None = None,
        reader: ProtocolLedgerReader | None = None,
        host_name: str | None = None,
        *,
        retry_sleep_s: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        writer_principals: Iterable[str] | None = None,
        operator_principal: str | None = None,
        writers: WriterPrincipals | None = None,
        append_lock: ProtocolLedgerAppendLock | None = None,
        append_if_absent: ModelAppendIfAbsentPolicy | None = None,
        relay_admission: ProtocolRelayAdmission | None = None,
        relay_rule: ModelRelayRequiresRule | None = None,
        acceptance_recorder: ProtocolJudgeAcceptanceRecorder | None = None,
        sequence_source: ProtocolJudgeSequenceSource | None = None,
        fenced_flip: ModelFencedFlipPolicy | None = None,
        authority: ProtocolLedgerAuthority | None = None,
        file_judge_restart: ProtocolFileJudgeRestart | None = None,
    ) -> None:
        # OMN-23154: the fenced flip is off unless the contract's policy is given. Without it the file
        # judge answers every append as it did before, and nothing below changes a receipt.
        self._flip_policy = fenced_flip
        self._flip_fence = (
            LedgerFlipFence(fenced_flip.drain_wait_seconds)
            if fenced_flip is not None
            else None
        )
        self._authority = authority
        self._file_judge_restart = file_judge_restart
        # OMN-23152: the judge records each row it accepts, with the sequence it gave, so the emit
        # gate can refuse a row the judge never saw. A bare construction records nothing.
        self._acceptance_recorder = acceptance_recorder
        self._sequence_source = sequence_source
        self._runner = runner
        self._relay_admission = relay_admission or CoreRelayAdmission()
        self._relay_rule = relay_rule or load_relay_requires_rule()
        self._reader = reader
        # The runtime boot resolver constructs a declared handler from injectable
        # params alone; the serve command supplies the real runner and reader, and
        # a bare construction refuses to append rather than guess a ledger.
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._retry_sleep_s = retry_sleep_s
        self._sleep = sleep
        # The operator principal is a known writer too: the serve process appends
        # the lab and delegation terminals it mirrors as that principal. The set is a
        # collaborator the serve can replace; a handler given none builds its own.
        self._operator_principal = operator_principal
        # The check-then-append step runs under this lock (OMN-22185). The serve gives
        # a flock beside the ledger; a bare construction gets the in-process adapter.
        self._append_lock: ProtocolLedgerAppendLock = (
            append_lock if append_lock is not None else InMemoryLedgerAppendLock()
        )
        # The contract's append_if_absent section; without it a request naming a row id
        # is refused, so no name or suffix lives in this module.
        self._append_if_absent = append_if_absent
        self._writers = (
            writers
            if writers is not None
            else WriterPrincipals(writer_principals or (), operator_principal)
        )

    @property
    def writers(self) -> WriterPrincipals:
        """The known writers; the serve process replaces them when the overlay changes (OMN-21622)."""
        return self._writers

    @property
    def flip_fence(self) -> LedgerFlipFence | None:
        """The fence of the authority handover; None when the fenced flip is not configured."""
        return self._flip_fence

    @property
    def authority(self) -> EnumLedgerAuthority:
        """Which judge answers an append now: the file judge until the flip."""
        if self._flip_fence is None:
            return EnumLedgerAuthority.FILE
        return self._flip_fence.authority

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    @property
    def host_name(self) -> str:
        return self._host

    def _require_runner(self) -> ProtocolLedgerAppendRunner:
        if self._runner is None:
            raise RuntimeError("work-ledger append handler has no append runner")
        return self._runner

    def _require_reader(self) -> ProtocolLedgerReader:
        if self._reader is None:
            raise RuntimeError("work-ledger append handler has no ledger reader")
        return self._reader

    def handle(
        self,
        request: ModelWorkLedgerAppendRequest,
        *,
        writer_principal: str | None = None,
    ) -> ModelWorkLedgerAppendReceipt:
        started = time.monotonic()
        authenticated_principal: str | None = None

        def receipt(
            status: EnumWorkLedgerAppendStatus,
            code: int,
            message: str,
            lines: list[int] | None = None,
            seqs: Sequence[int] | None = None,
        ) -> ModelWorkLedgerAppendReceipt:
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=status,
                exit_code=code,
                message=message[-2000:],
                ledger_lines=lines or [],
                ledger_seqs=list(seqs or []),
                ledger_host=self.host_name,
                duration_ms=int((time.monotonic() - started) * 1000),
                principal=authenticated_principal,
            )

        if writer_principal is None:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "unattributed: no broker principal produced this command; publish on your principal's writer topic",
            )
        if writer_principal not in self._writers:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                f"unknown writer principal {writer_principal}",
            )
        authenticated_principal = writer_principal

        rows: list[str] = []
        invalid_continuation = False
        for line in request.rows.splitlines():
            match = _ROW_START.match(line)
            if match:
                rows.append(match[1])
            elif line.strip() and (
                not rows
                or not line.startswith((" ", "\t"))
                or _ROW_START.match(line.lstrip())
            ):
                invalid_continuation = True
        if not rows:
            return receipt(EnumWorkLedgerAppendStatus.REFUSED, 65, "no ledger row")
        if invalid_continuation:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "non-row text must be an indented continuation, not a ledger row",
            )
        if any(row_type not in _BUS_ROW_TYPES for row_type in rows):
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED, 65, "unknown ledger row type"
            )
        relay_refusals = relay_admission_refusals(
            relay_rows(request.rows), self._relay_admission, self._relay_rule
        )
        if relay_refusals:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "\n".join((*relay_refusals, self._relay_rule.fix)),
            )
        if any(row_type in _AUTHORITY_TYPES for row_type in rows) and (
            authenticated_principal != self._operator_principal
        ):
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "RULING and OPERATOR-CONSENT require the configured operator principal",
            )
        id_cell: str | None = None
        if request.append_if_absent_row_id is not None:
            if self._append_if_absent is None:
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    "append_if_absent is not configured on this ledger host: "
                    "the contract's append_if_absent policy was not given to the handler",
                )
            id_cell = f"{self._append_if_absent.row_id_cell}={request.append_if_absent_row_id}"
            if len(rows) != 1 or not _request_lines(request.rows, id_cell):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    f"append_if_absent takes exactly one row carrying the whole pipe cell {id_cell}",
                )
        if not _REQUESTING_HOST.match(request.requesting_host):
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "requesting_host cannot be stamped as a via cell",
            )
        cell = f"req={request.request_id}"
        via_cell = VIA_BUS_PREFIX + request.requesting_host
        stamped_lines = request.rows.splitlines(keepends=True)
        legacy_lines = list(stamped_lines)
        row_index = 0
        for line_index, line in enumerate(stamped_lines):
            if not _ROW_START.match(line):
                continue
            row_index += 1
            # The local grammar splits on every pipe and strips each cell.
            # Authenticate the same cells so unspaced pipes cannot hide a
            # second principal from this boundary.
            fields = [field.strip() for field in line.rstrip("\r\n").split("|")]
            if not _request_lines(line, cell):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    f"row {row_index} ({fields[1]}) must carry the whole pipe cell {cell}",
                )
            principals = [
                field for field in fields[2:] if field.startswith("principal=")
            ]
            if principals and principals != [f"principal={authenticated_principal}"]:
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    "row principal conflicts with authenticated principal",
                )
            if principals and fields[-1].strip().startswith("principal="):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    "principal must be a header cell before row text",
                )
            if not principals:
                stamped_lines[line_index] = _principal_stamped(
                    line, authenticated_principal
                )
                legacy_lines[line_index] = _principal_stamped(
                    line, authenticated_principal, after_type=True
                )
        unstamped_rows = "".join(stamped_lines)
        unstamped_legacy = "".join(legacy_lines)
        stamped_rows = _via_stamped(unstamped_rows, cell, via_cell)
        legacy_with_via = _via_stamped(unstamped_legacy, cell, via_cell)
        earlier = (legacy_with_via, unstamped_rows, unstamped_legacy)
        if self._flip_fence is None:
            return self._file_append(
                request, receipt, rows, cell, id_cell, stamped_rows, earlier
            )
        try:
            with self._flip_fence.admit() as authority:
                duplicate = self._authority_duplicate(
                    request, receipt, stamped_rows, cell
                )
                if duplicate is not None:
                    return duplicate
                if authority is EnumLedgerAuthority.REDUCER:
                    return self._reducer_append(
                        request, receipt, id_cell, stamped_rows, cell
                    )
                return self._file_append(
                    request, receipt, rows, cell, id_cell, stamped_rows, earlier
                )
        except FenceClosedError:
            assert self._flip_policy is not None
            return receipt(
                EnumWorkLedgerAppendStatus.RETRY, 75, self._flip_policy.retry_message
            )

    def _file_append(
        self,
        request: ModelWorkLedgerAppendRequest,
        receipt: Callable[..., ModelWorkLedgerAppendReceipt],
        rows: list[str],
        cell: str,
        id_cell: str | None,
        stamped_rows: str,
        earlier_renderings: tuple[str, ...],
    ) -> ModelWorkLedgerAppendReceipt:
        """The file judge's path: the check-then-append step under the append lock."""
        try:
            with self._append_lock.hold():
                return self._append_locked(
                    request,
                    receipt,
                    rows,
                    cell,
                    id_cell,
                    stamped_rows,
                    earlier_renderings,
                )
        except LedgerLockTimeoutError as exc:
            return receipt(EnumWorkLedgerAppendStatus.ERROR, 75, str(exc))

    def _authority_duplicate(
        self,
        request: ModelWorkLedgerAppendRequest,
        receipt: Callable[..., ModelWorkLedgerAppendReceipt],
        stamped_rows: str,
        cell: str,
    ) -> ModelWorkLedgerAppendReceipt | None:
        """The answer to a request id the reducer already decided, from its records and never the file."""
        if self._authority is None:
            return None
        decided = self._authority.decision_for(str(request.request_id))
        if decided is None:
            return None
        requested = [
            work_ledger_row_id("\n".join(block).strip())
            for block in _request_blocks(stamped_rows, cell)
        ]
        held = [self._authority.row_id_at(seq) for seq in decided.ledger_seqs]
        if requested != held:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                f"request id {request.request_id} already belongs to different rows or principal",
            )
        return receipt(
            EnumWorkLedgerAppendStatus.DUPLICATE,
            0,
            "already appended",
            seqs=decided.ledger_seqs,
        )

    def _reducer_append(
        self,
        request: ModelWorkLedgerAppendRequest,
        receipt: Callable[..., ModelWorkLedgerAppendReceipt],
        id_cell: str | None,
        stamped_rows: str,
        cell: str,
    ) -> ModelWorkLedgerAppendReceipt:
        """After the flip: the reducer judges and sequences the rows; no file line is written."""
        authority = self._authority
        if authority is None:
            return receipt(
                EnumWorkLedgerAppendStatus.ERROR,
                70,
                "the reducer holds authority but no reducer is bound to this handler",
            )
        decision = authority.submit(
            str(request.request_id), stamped_rows, if_absent_cell=id_cell
        )
        if decision.verdict == "accepted":
            self._record_acceptance(request, stamped_rows, cell, decision.ledger_seqs)
            return receipt(
                EnumWorkLedgerAppendStatus.ACCEPTED,
                0,
                "",
                seqs=decision.ledger_seqs,
            )
        if decision.verdict == "duplicate":
            return receipt(
                EnumWorkLedgerAppendStatus.DUPLICATE,
                0,
                "already appended",
                seqs=decision.ledger_seqs,
            )
        if decision.verdict == "already-present":
            return receipt(
                EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
                0,
                f"already present: a row carries {id_cell}",
                seqs=decision.ledger_seqs,
            )
        return receipt(EnumWorkLedgerAppendStatus.REFUSED, 65, decision.reason)

    def flip_to_reducer(self) -> ModelLedgerFlipResult:
        """Fence, drain every in-flight append, check the file's last row names the reducer's, switch."""
        fence, authority = self._require_flip()
        if fence.authority is EnumLedgerAuthority.REDUCER:
            return ModelLedgerFlipResult(
                authority=EnumLedgerAuthority.REDUCER, fence_seq=authority.highest_seq()
            )

        def switch() -> int:
            fence_seq = authority.highest_seq()
            file_last = _last_row_id(self._require_reader().read_text())
            held_last = authority.row_id_at(fence_seq)
            if file_last != held_last:
                raise FlipRefusedError(
                    f"the file's last row ({file_last}) and the reducer's row at ledger_seq "
                    f"{fence_seq} ({held_last}) are not the same row; nothing was switched"
                )
            return fence_seq

        fence_seq = fence.handover(EnumLedgerAuthority.REDUCER, switch)
        return ModelLedgerFlipResult(
            authority=EnumLedgerAuthority.REDUCER, fence_seq=fence_seq
        )

    def rollback_to_file(self) -> ModelLedgerFlipResult:
        """Fence, drain, restart the file judge from the reducer's records, switch; the file is not written."""
        fence, authority = self._require_flip()
        restart = self._file_judge_restart
        if restart is None:
            raise RuntimeError("no file judge restart is bound to this handler")
        if fence.authority is EnumLedgerAuthority.FILE:
            return ModelLedgerFlipResult(
                authority=EnumLedgerAuthority.FILE, fence_seq=authority.highest_seq()
            )

        def switch() -> int:
            state = ModelFileJudgeRestartState(
                fence_seq=authority.highest_seq(),
                decided_request_ids=authority.decided_request_ids(),
            )
            restart.restart(state)
            return state.fence_seq

        fence_seq = fence.handover(EnumLedgerAuthority.FILE, switch)
        return ModelLedgerFlipResult(
            authority=EnumLedgerAuthority.FILE, fence_seq=fence_seq
        )

    def _require_flip(self) -> tuple[LedgerFlipFence, ProtocolLedgerAuthority]:
        if self._flip_fence is None or self._authority is None:
            raise RuntimeError(
                "the fenced flip is not configured: bind the contract's fenced_flip policy and a reducer"
            )
        return self._flip_fence, self._authority

    def _append_locked(
        self,
        request: ModelWorkLedgerAppendRequest,
        receipt: Callable[..., ModelWorkLedgerAppendReceipt],
        rows: list[str],
        cell: str,
        id_cell: str | None,
        stamped_rows: str,
        earlier_renderings: tuple[str, ...],
    ) -> ModelWorkLedgerAppendReceipt:
        """The check-then-append step; the caller holds the append lock."""
        # Every row of one request carries the same req= cell, so the request is
        # complete only when all of them are on the ledger. A partial count is an
        # error a person must reconcile, never a silent duplicate (model review,
        # omnibase_internal tla/ledger_bus_append/REVIEW.md).
        landed = _request_lines(self._require_reader().read_text(), cell)
        if len(landed) == len(rows):
            existing_rows = _request_blocks(self._require_reader().read_text(), cell)
            requested_rows = _request_blocks(stamped_rows, cell)
            # A request that landed before OMN-19513 carries principal= right
            # after the type; its replay is the same request, not a conflict.
            # A request that landed before OMN-24205 has no via=bus: cell; its
            # replay is the same request, not a conflict.
            if existing_rows not in (
                requested_rows,
                *(_request_blocks(text, cell) for text in earlier_renderings),
            ):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    f"request id {request.request_id} already belongs to different rows or principal: "
                    f"ledger digest {blocks_digest(existing_rows)}, request digest {blocks_digest(requested_rows)}",
                )
            # A redelivered request is answered, and its rows stay accepted after a restart.
            self._record_acceptance(request, stamped_rows, cell)
            return receipt(
                EnumWorkLedgerAppendStatus.DUPLICATE, 0, "already appended", landed
            )
        if landed:
            return receipt(
                EnumWorkLedgerAppendStatus.ERROR,
                70,
                f"partial append: {len(landed)} of {len(rows)} rows carrying {cell} are "
                "on the ledger; reconcile by hand before the request is retried",
                landed,
            )
        if id_cell is not None:
            present = _request_lines(self._require_reader().read_text(), id_cell)
            if present:
                return receipt(
                    EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
                    0,
                    f"already present: a row carries {id_cell}",
                    present,
                )
        for attempt in range(3):
            result = self._require_runner().append(stamped_rows)
            if result.exit_code != 75 or attempt == 2:
                break
            self._sleep(self._retry_sleep_s)
        tail = result.stderr or result.stdout
        landed = _request_lines(self._require_reader().read_text(), cell)
        if result.exit_code == 0 or len(landed) == len(rows):
            # A non-zero exit after the rows landed (the stranded-clone signal, 78)
            # still appended them: the receipt says accepted and carries the text.
            self._record_acceptance(request, stamped_rows, cell)
            return receipt(EnumWorkLedgerAppendStatus.ACCEPTED, 0, tail, landed)
        status = (
            EnumWorkLedgerAppendStatus.REFUSED
            if result.exit_code == 65
            else EnumWorkLedgerAppendStatus.ERROR
        )
        return receipt(status, result.exit_code, tail)

    def _record_acceptance(
        self,
        request: ModelWorkLedgerAppendRequest,
        stamped_rows: str,
        cell: str,
        seqs: Sequence[int] | None = None,
    ) -> None:
        """Write one acceptance per accepted row the judge gave a sequence (OMN-23152).

        ``seqs`` are the reducer's, in row order, after the flip; before it the file judge's sequence
        source names each row's. A file-judge acceptance is also handed to the reducer (OMN-23154), so
        it knows every request the file judge answered when authority moves."""
        if self._acceptance_recorder is None:
            return
        blocks = _request_blocks(stamped_rows, cell)
        if seqs is None and self._sequence_source is None:
            return
        accepted: list[ModelWorkLedgerJudgeAcceptance] = []
        for index, block in enumerate(blocks):
            row_id = work_ledger_row_id("\n".join(block).strip())
            if seqs is not None:
                seq: int | None = seqs[index] if index < len(seqs) else None
            else:
                assert self._sequence_source is not None
                seq = self._sequence_source.sequence_of(row_id)
            if seq is not None:
                acceptance = ModelWorkLedgerJudgeAcceptance(
                    row_id=row_id, ledger_seq=seq, request_id=request.request_id
                )
                self._acceptance_recorder.record(acceptance)
                accepted.append(acceptance)
        if seqs is None and self._authority is not None and accepted:
            self._authority.adopt(accepted)


def terminal_refused(
    request: ModelWorkLedgerAppendRequest,
    receipt: ModelWorkLedgerAppendReceipt,
    refused_at: datetime,
) -> ModelWorkLedgerTerminalRefused | None:
    """The TERMINAL_REFUSED event for a refused request that carries TERMINAL rows.

    A refused TERMINAL leaves the CLAIM it closes open on the ledger; the event
    names the lanes, tickets and PRs of those rows so the refusal is loud.
    """
    if receipt.status is not EnumWorkLedgerAppendStatus.REFUSED:
        return None
    terminals = 0
    lanes: list[str] = []
    tickets: list[str] = []
    prs: list[str] = []
    for line in request.rows.splitlines():
        match = _ROW_START.match(line)
        if match is None or match[1].strip() != "TERMINAL":
            continue
        terminals += 1
        for field in (field.strip() for field in line.split("|")[2:]):
            name, _, value = field.partition("=")
            if not value:
                continue
            if name == "lane":
                lanes.append(value)
            elif name == "ticket":
                tickets.append(value)
            elif name == "pr":
                prs.extend(pr.strip() for pr in value.split(",") if pr.strip())
    if not terminals:
        return None
    return ModelWorkLedgerTerminalRefused(
        request_id=request.request_id,
        requested_by_lane=request.requested_by_lane,
        requesting_host=request.requesting_host,
        ledger_host=receipt.ledger_host,
        principal=receipt.principal,
        exit_code=receipt.exit_code,
        reason=receipt.message,
        terminal_lanes=tuple(dict.fromkeys(lanes)),
        tickets=tuple(dict.fromkeys(tickets)),
        prs=tuple(dict.fromkeys(prs)),
        refused_at=refused_at,
    )


def _last_row_id(text: str) -> str | None:
    """The content hash of the file's last row with its continuations, or None for a file with no row."""
    block: list[str] = []
    for line in text.splitlines():
        if _ROW_START.match(line):
            block = [line]
        elif block and line.strip():
            block.append(line)
    return work_ledger_row_id("\n".join(block).strip()) if block else None


def _request_lines(text: str, cell: str) -> list[int]:
    """Match the whole pipe cell, including a last cell without a trailing pipe."""
    pattern = re.compile(r" \| " + re.escape(cell) + r"(?= \||$)")
    return [
        index
        for index, line in enumerate(text.splitlines(), start=1)
        if _ROW_START.match(line) and pattern.search(line)
    ]


def blocks_digest(blocks: list[list[str]]) -> str:
    """The sha256 of a request's rows as one text, so a clash names what each side holds."""
    text = "\n".join(line for block in blocks for line in block)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request_blocks(text: str, cell: str) -> list[list[str]]:
    """Compare content and attribution on redelivery, including continuations."""
    blocks: list[list[str]] = []
    selected = False
    for line in text.splitlines():
        if _ROW_START.match(line):
            selected = bool(_request_lines(line, cell))
            if selected:
                blocks.append([line])
        elif selected:
            blocks[-1].append(line)
    for block in blocks:
        while block and not block[-1]:
            block.pop()
    return blocks
