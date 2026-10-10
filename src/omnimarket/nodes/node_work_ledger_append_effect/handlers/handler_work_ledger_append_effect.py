# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate, deduplicate and append ledger rows; no bus code (OMN-20275)."""

import base64
import binascii
import re
import socket
import time
from collections.abc import Callable, Mapping
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerAppendRunner,
    ProtocolLedgerReader,
)

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ([^|\r\n]+) \| ")
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


class HandlerWorkLedgerAppendEffect:
    def __init__(
        self,
        runner: ProtocolLedgerAppendRunner | None = None,
        reader: ProtocolLedgerReader | None = None,
        host_name: str | None = None,
        *,
        retry_sleep_s: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        public_keys: Mapping[str, Ed25519PublicKey] | None = None,
        operator_principal: str | None = None,
    ) -> None:
        self._runner = runner
        self._reader = reader
        # The runtime boot resolver constructs a declared handler from injectable
        # params alone; the serve command supplies the real runner and reader, and
        # a bare construction refuses to append rather than guess a ledger.
        self._host = host_name or socket.gethostname().split(".", 1)[0]
        self._retry_sleep_s = retry_sleep_s
        self._sleep = sleep
        self._public_keys = dict(public_keys or {})
        self._operator_principal = operator_principal

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
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        started = time.monotonic()
        authenticated_principal: str | None = None

        def receipt(
            status: EnumWorkLedgerAppendStatus,
            code: int,
            message: str,
            lines: list[int] | None = None,
        ) -> ModelWorkLedgerAppendReceipt:
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=status,
                exit_code=code,
                message=message[-2000:],
                ledger_lines=lines or [],
                ledger_host=self.host_name,
                duration_ms=int((time.monotonic() - started) * 1000),
                principal=authenticated_principal,
            )

        # Lane, host, principal and all row text are signed together. The issuer's
        # verification keys and operator identity come from the serve process,
        # never from the command or its bus envelope (OMN-20282).
        key = self._public_keys.get(request.principal or "")
        if key is None or request.signature is None:
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "unsigned or unknown principal: configure an issuer-recorded signing identity",
            )
        try:
            key.verify(
                base64.b64decode(request.signature, validate=True),
                request.signing_bytes(),
            )
        except (InvalidSignature, ValueError, binascii.Error):
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "request signature does not verify",
            )
        authenticated_principal = request.principal

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
        if any(row_type in _AUTHORITY_TYPES for row_type in rows) and (
            authenticated_principal != self._operator_principal
        ):
            return receipt(
                EnumWorkLedgerAppendStatus.REFUSED,
                65,
                "RULING and OPERATOR-CONSENT require the configured operator principal",
            )
        cell = f"req={request.request_id}"
        stamped_lines = request.rows.splitlines(keepends=True)
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
                # Preserve the caller's text, continuations and line endings.
                timestamp, row_type, rest = line.split(" | ", 2)
                stamped_lines[line_index] = (
                    f"{timestamp} | {row_type} | principal={authenticated_principal} | {rest}"
                )
        stamped_rows = "".join(stamped_lines)
        # Every row of one request carries the same req= cell, so the request is
        # complete only when all of them are on the ledger. A partial count is an
        # error a person must reconcile, never a silent duplicate (model review,
        # omnibase_internal tla/ledger_bus_append/REVIEW.md).
        landed = _request_lines(self._require_reader().read_text(), cell)
        if len(landed) == len(rows):
            existing_rows = _request_blocks(self._require_reader().read_text(), cell)
            if existing_rows != _request_blocks(stamped_rows, cell):
                return receipt(
                    EnumWorkLedgerAppendStatus.REFUSED,
                    65,
                    "request id already belongs to different rows or principal",
                )
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
            return receipt(EnumWorkLedgerAppendStatus.ACCEPTED, 0, tail, landed)
        status = (
            EnumWorkLedgerAppendStatus.REFUSED
            if result.exit_code == 65
            else EnumWorkLedgerAppendStatus.ERROR
        )
        return receipt(status, result.exit_code, tail)


def _request_lines(text: str, cell: str) -> list[int]:
    """Match the whole pipe cell, including a last cell without a trailing pipe."""
    pattern = re.compile(r" \| " + re.escape(cell) + r"(?= \||$)")
    return [
        index
        for index, line in enumerate(text.splitlines(), start=1)
        if _ROW_START.match(line) and pattern.search(line)
    ]


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
