# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J5 join's three real ports (OMN-23086): the secret store's identity, the ledger append and the projection's row.

Both are adapters of the ports in ``protocols/join_j5.py``; the in-memory ones stay the default and the overlay
chooses these. Nothing here holds a setting: the store handle, the principal, the attempts and the waits are the
overlay's, passed in.

``StoreProbeIdentities`` answers piece 5's side: the identity reference names a folder and a key in the secret store and
the principal an append runs under. It resolves only when the store resolves that key in the identity's scope (the
same probe piece 5 reads every consumer through); the value the store returns is never read, kept or printed.

``SourceProjectedRows`` answers piece 6's side: the projected row, found by asking a ledger row source (the work-ledger
read command, which reads the projection) for the durable row's own text. A row the projection has not heard yet is
asked for again, up to the attempts the overlay sets; a source that errors reads as no projected row.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.model_secret_reading import (
    EnumSecretProbeOutcome,
    ModelSecretHandle,
)
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_secret_reading_effect.protocols import (
    ProtocolSecretProbeStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    StoreIdentityUnresolvedError,
)


class BusCliAppend:
    """Piece 6's real append: the work-ledger bus append command, which prints the ledger host's own receipt.

    The broker login the command runs under is the writer identity; the receipt names the principal that authenticated.
    The join's rule is that this principal is the one the store resolved, so a receipt authenticated as anyone else is not
    an accepted append, whatever its status says. The rows go to the command through a file, never a command line.
    """

    def __init__(
        self,
        argv_prefix: Sequence[str],
        *,
        host: str,
        timeout_seconds: float,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._prefix = list(argv_prefix)
        self._host = host
        self._timeout = timeout_seconds
        self._run = run

    def handle(
        self,
        request: ModelWorkLedgerAppendRequest,
        *,
        writer_principal: str | None = None,
    ) -> ModelWorkLedgerAppendReceipt:
        def refused(message: str) -> ModelWorkLedgerAppendReceipt:
            return ModelWorkLedgerAppendReceipt(
                request_id=request.request_id,
                status=EnumWorkLedgerAppendStatus.ERROR,
                exit_code=70,
                message=message,
                ledger_lines=[],
                ledger_host=self._host,
                duration_ms=0,
            )

        with tempfile.TemporaryDirectory() as scratch:
            rows_file = Path(scratch) / "rows.txt"
            rows_file.write_text(request.rows, encoding="utf-8")
            argv = [
                *self._prefix,
                "--lane",
                request.requested_by_lane,
                "--host",
                self._host,
                "--request-id",
                str(request.request_id),
                "--rows-file",
                str(rows_file),
                "--timeout-s",
                str(self._timeout),
            ]
            try:
                done = self._run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout + 15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return refused(f"append command failed: {type(exc).__name__}")
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            receipt = ModelWorkLedgerAppendReceipt.model_validate_json(lines[-1])
        except (IndexError, ValueError):
            return refused(f"append command exit {done.returncode} gave no receipt")
        if (
            receipt.status
            in (
                EnumWorkLedgerAppendStatus.ACCEPTED,
                EnumWorkLedgerAppendStatus.DUPLICATE,
                EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
            )
            and receipt.principal != writer_principal
        ):
            return receipt.model_copy(
                update={
                    "status": EnumWorkLedgerAppendStatus.REFUSED,
                    "message": "the receipt's authenticated principal is not the identity the store resolved",
                }
            )
        return receipt


class ModelStoreIdentityBinding(BaseModel):
    """One identity reference: where its key lives in the store and the principal it stands for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)
    folder: str = Field(min_length=1, pattern=r"^/")
    key: str = Field(min_length=1)
    principal: str = Field(min_length=1)


class StoreProbeIdentities:
    def __init__(
        self,
        store: ProtocolSecretProbeStore,
        bindings: tuple[ModelStoreIdentityBinding, ...],
    ) -> None:
        self._store = store
        self._bindings: Mapping[str, ModelStoreIdentityBinding] = {
            b.ref: b for b in bindings
        }

    def resolve(self, identity_ref: str) -> str:
        binding = self._bindings.get(identity_ref)
        if binding is None:
            raise StoreIdentityUnresolvedError(
                f"no store binding is declared for {identity_ref}"
            )
        outcome = self._store.resolve(
            ModelSecretHandle(
                consumer=binding.ref, folder=binding.folder, key=binding.key
            )
        ).outcome
        if outcome is not EnumSecretProbeOutcome.RESOLVED:
            raise StoreIdentityUnresolvedError(
                f"the store did not resolve the key of {identity_ref}: {outcome.value}"
            )
        return binding.principal


class SourceProjectedRows:
    def __init__(
        self,
        source: ProtocolLedgerRowSource,
        *,
        attempts: int,
        interval_seconds: float,
        slack_seconds: int,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._attempts = attempts
        self._interval = interval_seconds
        self._slack = timedelta(seconds=slack_seconds)
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))

    def read_row(
        self, *, ledger_id: str, ledger_seq: int, raw_row: str | None = None
    ) -> ModelWorkLedgerRowRecord | None:
        if raw_row is None:
            return None
        expected = evidence_row_from_text(raw_row)
        if expected is None:
            return None
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._interval)
            read = self._source.read(
                ModelLedgerQuery(
                    since=expected.stamp - self._slack,
                    until=self._clock() + self._slack,
                    row_type=expected.row_type,
                    lane=expected.lane,
                    term=raw_row.strip(),
                )
            )
            if read.error is not None:
                continue
            for row in read.rows:
                if row.raw.strip() == raw_row.strip():
                    return ModelWorkLedgerRowRecord(
                        row_id=work_ledger_row_id(row.raw),
                        ledger_id=ledger_id,
                        row_ts=row.stamp,
                        row_type=row.row_type,
                        row_lane=row.lane,
                        tickets=(),
                        raw_row=row.raw,
                        source="join-j5-projection-read",
                        ledger_seq=ledger_seq,
                    )
        return None
