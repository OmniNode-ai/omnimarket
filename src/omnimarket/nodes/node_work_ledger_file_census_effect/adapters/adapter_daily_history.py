# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dated daily records of the census, kept as one JSON file in the unit's state directory."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusDailyRecord,
)


class JsonDailyRecordHistory:
    """``{"records": {"<UTC date>": <daily record>}}``; the last record of a day is the day's; ``keep_days`` days are kept."""

    def __init__(self, path: Path, keep_days: int) -> None:
        if keep_days < 1:
            raise ValueError("keep_days must be at least 1")
        self._path = path
        self._keep_days = keep_days

    def _read(self) -> dict[str, ModelCensusDailyRecord]:
        if not self._path.exists():
            return {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                day: ModelCensusDailyRecord.model_validate(record)
                for day, record in document["records"].items()
            }
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise ValueError(
                f"daily record history unreadable: {type(exc).__name__}"
            ) from exc

    def previous_before(self, recorded_on: str) -> ModelCensusDailyRecord | None:
        earlier = [day for day in self._read() if day < recorded_on]
        return self._read()[max(earlier)] if earlier else None

    def keep(self, record: ModelCensusDailyRecord) -> None:
        try:
            records = self._read()
        except ValueError:
            records = {}
        records[record.recorded_on] = record
        newest = sorted(records)[-self._keep_days :]
        document = {
            "records": {day: records[day].model_dump(mode="json") for day in newest}
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        held = self._path.with_suffix(self._path.suffix + ".tmp")
        held.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        held.replace(self._path)
