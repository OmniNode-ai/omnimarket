# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live readings feeder: the ledger's reading rows folded into the file the projection reads (OMN-23427).

The projection's readings source (``read_readings`` in runtime_work_state) reads a JSON-lines file under the registry
home; nothing wrote it, so this feeder does, from the reading rows the reading writer appends to the ledger. Every path
and field name comes from node_work_state_projection_effect's contract ``runtime:`` block, never a literal here. The
newest row of a key wins, ordered by the row's published cell's ISO time when it parses, else by the row's ledger stamp.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.work_state_feeder import ModelFeederOutcome
from omnimarket.models.work_state_feeder.model_readings_feed_rules import (
    ModelReadingsFeedRules,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    runtime_section,
)

RULES_OVERLAY_ID = "node_work_state_readings_feeder_scheduled_effect_rules"
RULES_FILE_ENV = "ONEX_WORK_STATE_READINGS_FEED_RULES"

NO_ROWS_CAUSE = "no reading rows in the window"
NO_VALUES_CAUSE = "no reading values in the window"


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value) if value else None


def _row_time(row: ModelLedgerEvidenceRow, rules: ModelReadingsFeedRules) -> datetime:
    """The row's own time: the published cell's ISO time when it parses, else the ledger stamp."""
    text = row.cells.get(rules.published_cell, "")
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
    return row.stamp


class ReadingsFeeder:
    """One feed: the ledger's reading rows of the window, folded to one line per key, written whole.

    The write is atomic (a temp file beside the target, then a replace), so the projection never reads
    a half-written file. A feed with nothing to write leaves the old file untouched.
    """

    def __init__(
        self,
        rules: ModelReadingsFeedRules,
        ledger: ProtocolLedgerRowSource | None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._rules = rules
        self._ledger = ledger
        self._environ = environ

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None) -> ReadingsFeeder:
        """The live profile's feeder: the rules and the ledger route from the environment's overlays.

        No ledger path is a feeder that fails naming the variable, not a crash at build time: the
        schedule runs it either way.
        """
        env = os.environ if environ is None else environ
        rules = load_structured_overlay(
            RULES_OVERLAY_ID,
            ModelReadingsFeedRules,
            env,
            file_env=RULES_FILE_ENV,
        )
        rt = runtime_section()
        live = _env_path(env, rt["ledger"]["path_env"])
        ledger = (
            FileLedgerRowSource(live, live.parent / rt["ledger"]["archive_subdir"])
            if live is not None
            else None
        )
        return cls(rules, ledger, env)

    def feed(self, now: datetime) -> ModelFeederOutcome:
        rt = runtime_section()
        rules = self._rules
        if self._ledger is None:
            return ModelFeederOutcome(
                status="failed",
                item_count=0,
                cause=f"{rt['ledger']['path_env']} unset",
            )
        read = self._ledger.read(
            ModelLedgerQuery(
                since=now - timedelta(hours=rules.lookback_hours),
                until=now + timedelta(days=1),
                row_type=rules.row_type,
                term=rules.term,
            )
        )
        if read.error is not None:
            return ModelFeederOutcome(status="failed", item_count=0, cause=read.error)
        newest: dict[str, tuple[datetime, ModelLedgerEvidenceRow]] = {}
        for row in read.rows:
            key = row.cells.get(rules.key_cell)
            if not key:
                continue
            when = _row_time(row, rules)
            held = newest.get(key)
            if held is None or when > held[0]:
                newest[key] = (when, row)
        lines: list[str] = []
        source_as_of: datetime | None = None
        for key, (when, row) in sorted(newest.items()):
            value = row.cells.get(rules.value_cell)
            if value is None:
                continue
            doc = {
                rt["readings"]["key_field"]: key,
                rt["readings"]["value_field"]: value,
                "read_at": when.isoformat(),
            }
            lines.append(json.dumps(doc, ensure_ascii=False, sort_keys=True))
            if source_as_of is None or when > source_as_of:
                source_as_of = when
        if not lines:
            rows_seen = bool(newest)
            return ModelFeederOutcome(
                status="not_fed",
                item_count=0,
                cause=NO_VALUES_CAUSE if rows_seen else NO_ROWS_CAUSE,
            )
        target = Path(self._env(rt["home_env"])) / rt["readings"]["relpath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        # One temp file in the target's directory, then a whole replace: the projection never reads a half-written file.
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("".join(line + "\n" for line in lines))
            Path(tmp_name).replace(target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return ModelFeederOutcome(
            status="fed", source_as_of=source_as_of, item_count=len(lines)
        )

    def _env(self, name: str) -> str:
        env = os.environ if self._environ is None else self._environ
        return env[name]


__all__ = ["ReadingsFeeder"]
