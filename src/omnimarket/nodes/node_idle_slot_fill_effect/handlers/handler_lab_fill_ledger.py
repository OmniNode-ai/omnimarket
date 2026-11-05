# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger facts for idle-slot fill, through the declared query publisher when configured."""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.node_idle_slot_fill_effect import overlay
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_lab_fill_ledger import (
    ModelLabFillLedgerRequest,
    ModelLabFillLedgerResult,
)


def _cell(text: str, name: str) -> str:
    match = re.search(rf"(?:^|\|)\s*{name}=([^|]*)", text)
    return match[1].strip() if match else ""


def _stamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


class HandlerLabFillLedger:
    """Keep the claim/readback fold; replace file acquisition with the configured publisher.

    A configured publisher that fails raises. It never falls back to the large
    ledger, nor reports a failed read as an empty claim set.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if environ is None else environ

    def handle(self, request: ModelLabFillLedgerRequest) -> ModelLabFillLedgerResult:
        now = request.now
        spec = importlib.util.spec_from_file_location(
            "_lab_fill_drain_map", request.drain_map
        )
        if spec is None or spec.loader is None:
            raise ValueError("cannot load drain_map")
        dm = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = dm
        spec.loader.exec_module(dm)
        grammar = dm.load_grammar(request.registry_root)
        command, timeout = overlay.ledger_read(self._env)
        if command:
            query = ModelWorkLedgerQueryRequest(
                correlation_id=uuid4(),
                query=EnumWorkLedgerQueryKind.ROWS,
                since=now - timedelta(days=2),
                until=now,
                now=now,
            )
            answer = self._query(query, command, timeout)
            rows = [
                dm.Row(
                    index,
                    row.row_ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    row.row_type,
                    row.row_lane,
                    row.raw_row,
                    row.source,
                )
                for index, row in enumerate(answer.rows)
            ]
        else:
            if (
                self._env.get("ONEX_LEDGER_FILE_RETIRED") == "1"
                or self._env.get("ONEX_LEDGER_RECORD") == "bus"
            ):
                raise ValueError(
                    "lab-fill ledger read needs ONEX_LEDGER_BUS_READ_COMMAND or idle_slot_fill.ledger_read_command"
                )
            rows, _ = dm.load_rows(
                request.registry_root, 2.0, now, grammar, request.ledger
            )
        opened = dm.open_claims(rows, grammar, now - timedelta(days=2))
        last: dict[str, datetime] = {}
        for row in rows:
            stamp = _stamp(row.ts[:20])
            if row.lane and stamp is not None:
                stamp = stamp.replace(tzinfo=UTC)
                last[row.lane] = max(last.get(row.lane, stamp), stamp)
        claimed = {
            tid
            for row in opened
            if row.lane in last and now - last[row.lane] <= timedelta(hours=2)
            for tid in re.findall(r"\bOMN-\d+\b", row.text)
        }
        readbacks: dict[str, str] = {}
        for row in rows:
            if row.rtype != "TERMINAL":
                continue
            # a fill lane rides on the carrier ticket and names the generated one only in its lane (fill-readback-OMN-n-...)
            ids = next(
                (
                    ids
                    for text in (
                        *(
                            _cell(row.text, name)
                            for name in ("subject", "related", "blocker")
                        ),
                        row.lane,
                        _cell(row.text, "ticket"),
                    )
                    if (
                        ids := [
                            t.upper()
                            for t in re.findall(
                                r"\bOMN-\d+\b", text or "", re.IGNORECASE
                            )
                        ]
                    )
                ),
                [],
            )
            if not ids:
                continue
            # a fill lane that blocked on the readback too settles it: sent again, it would end the same way
            if (
                _cell(row.text, "outcome") == "blocked"
                and "readback" in _cell(row.text, "blocker").lower()
                and not row.lane.startswith("fill-")
            ):
                readbacks[ids[0]] = row.text[:300]
            else:
                readbacks.pop(ids[0], None)
        return ModelLabFillLedgerResult(claimed=frozenset(claimed), readbacks=readbacks)

    def _query(
        self,
        request: ModelWorkLedgerQueryRequest,
        command: str,
        timeout: float,
    ) -> ModelWorkLedgerQueryResult:
        try:
            argv = shlex.split(command)
            if not argv:
                raise ValueError("empty ledger query command")
            done = subprocess.run(
                argv,
                input=request.model_dump_json(exclude_none=True),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=dict(self._env),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                f"lab-fill ledger query unread: {type(exc).__name__}"
            ) from exc
        if done.returncode:
            raise ValueError(f"lab-fill ledger query exited {done.returncode}")
        try:
            # Publishers may print diagnostics before their final JSON answer.
            answer = ModelWorkLedgerQueryResult.model_validate_json(
                done.stdout.strip().splitlines()[-1]
            )
        except (ValueError, IndexError) as exc:
            raise ValueError("lab-fill ledger query returned no typed answer") from exc
        if (
            answer.correlation_id != request.correlation_id
            or answer.query != request.query
        ):
            raise ValueError("lab-fill ledger query answered another request")
        if answer.error or answer.status == "error":
            raise ValueError("lab-fill ledger query answered with an error")
        if answer.matched != len(answer.rows) or answer.next_cursor:
            raise ValueError("lab-fill ledger query returned incomplete rows")
        return answer
