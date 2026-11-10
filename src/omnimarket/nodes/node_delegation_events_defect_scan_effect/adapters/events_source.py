# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows of delegation_events for the defect scan: from given rows (the in-memory default) or from the dev postgres over ssh.

The ssh backend runs one read only transaction in the postgres container of the overlay's compose project and prints one JSON
object per line: the database clock first, then one object per row of the day. Nothing but the eight columns the scan needs
leaves the database: no prompt, no answer and no identifier.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanFact
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    Runner,
    run_process,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    ModelDelegationEventsSource,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.protocols import (
    ModelDefectEventsRead,
)


class InMemoryDefectEventSource:
    """Reads given rows at a given time, or fails with a given reason."""

    def __init__(
        self,
        now: datetime | None = None,
        events: Sequence[ModelDefectScanFact] = (),
        error: str | None = None,
    ) -> None:
        self._now = now
        self._events = tuple(events)
        self._error = error

    def read(self, window_seconds: int) -> ModelDefectEventsRead:
        if self._error is not None:
            return ModelDefectEventsRead(error=self._error)
        return ModelDefectEventsRead(now=self._now, events=self._events)


def _cost(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("cost is not a number") from exc


def parse_rows(text: str) -> ModelDefectEventsRead:
    """The clock line and the row lines of the ssh read, or the typed reason they were not usable."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ModelDefectEventsRead(error="rows-answer-empty")
    try:
        clock = json.loads(lines[0])
        now = datetime.fromisoformat(clock["now"])
        events = tuple(
            ModelDefectScanFact(
                event_time=datetime.fromisoformat(item["t"]),
                data_source=item["src"],
                is_shadow=item["shadow"],
                terminal_ok=item["ok"],
                cost_usd=_cost(item["cost"]),
                cost_measurement_source=item["cms"],
                tokens_total=item["tok"],
                caller_lane=item["lane"],
            )
            for item in map(json.loads, lines[1:])
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError):
        return ModelDefectEventsRead(error="rows-answer-malformed")
    if now.utcoffset() is None:
        return ModelDefectEventsRead(error="rows-clock-without-zone")
    return ModelDefectEventsRead(now=now, events=events)


class SshPostgresDefectEventSource:
    """Reads the day's rows in a read only transaction inside the postgres container, over ssh."""

    def __init__(
        self,
        source: ModelDelegationEventsSource,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._source = source
        self._environ = environ
        self._run = run

    def script(self, window_seconds: int) -> str:
        s = self._source
        sql = "\n".join(
            [
                "BEGIN READ ONLY;",
                "SELECT json_build_object('now', to_char(now() AT TIME ZONE 'UTC', "
                + '\'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"\'))::text;',
                "SELECT json_build_object("
                "'t', to_char(\"timestamp\" AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'), "
                "'src', data_source, 'shadow', is_shadow, 'ok', terminal_ok, "
                "'cost', cost_usd::text, 'cms', cost_measurement_source, "
                "'tok', COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0), "
                "'lane', caller_lane)::text "
                f"FROM delegation_events WHERE \"timestamp\" > now() - interval '{int(window_seconds)} seconds' "
                'ORDER BY "timestamp";',
                "COMMIT;",
            ]
        )
        return "\n".join(
            [
                "set -eu",
                "DB=$(docker ps --filter "
                f"label=com.docker.compose.project={s.compose_project} --filter "
                f"label=com.docker.compose.service={s.service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$DB" ]',
                f"docker exec -i \"$DB\" psql -X -q -v ON_ERROR_STOP=1 -U {s.db_user} -d {s.database} -tA <<'SQL'",
                sql,
                "SQL",
            ]
        )

    def read(self, window_seconds: int) -> ModelDefectEventsRead:
        host = self._environ.get(self._source.ssh_host_env, "").strip()
        if not host:
            return ModelDefectEventsRead(error="ssh-host-variable-unset")
        try:
            done = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                self.script(window_seconds).encode(),
                self._source.read_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ModelDefectEventsRead(error="rows-read-timeout")
        except OSError as exc:
            return ModelDefectEventsRead(
                error=f"rows-read-unstartable-{type(exc).__name__}"
            )
        if done.returncode:
            return ModelDefectEventsRead(error=f"rows-read-exit-{done.returncode}")
        return parse_rows(done.stdout.decode("utf-8", errors="replace"))


__all__ = [
    "InMemoryDefectEventSource",
    "SshPostgresDefectEventSource",
    "parse_rows",
]
