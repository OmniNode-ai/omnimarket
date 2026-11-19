# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the integration ledger's NOT RUNNING section into the integration pass models."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)

_SECTION = re.compile(r"## NOT RUNNING(?:\s+\(.*\))?")
_ROW = re.compile(
    r"- (?P<repository>[A-Za-z0-9_.-]+) (?P<number>[0-9]+)"
    r" \| (?P<title>.+?) \| merged (?P<merged>\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z)"
    r" \| runs: *(?P<target>.+?) \| state: NOT RUNNING(?: \| (?P<details>.*))?"
)
_DETAIL_SEPARATOR = re.compile(r" \| (?=(?:gap|step|owner):)")


def read_not_running(
    text: str, *, as_of: datetime, origin: str
) -> ModelIntegrationReadingBatch:
    """Read numbered entries in the NOT RUNNING section in ledger order."""
    readings: list[ModelIntegrationReading] = []
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            if in_section:
                break
            in_section = _SECTION.fullmatch(line) is not None
            continue
        if not in_section:
            continue

        match = _ROW.fullmatch(line)
        if match is None:
            continue
        number = int(match["number"])
        title = match["title"].strip()
        target = match["target"].strip()
        if number < 1 or not title or not target:
            continue
        try:
            merged_at = datetime.strptime(match["merged"], "%Y-%m-%d %H:%MZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        details = {}
        for cell in _DETAIL_SEPARATOR.split(match["details"] or ""):
            key, _, value = cell.partition(":")
            details[key] = value.strip()
        readings.append(
            ModelIntegrationReading(
                repository=match["repository"],
                number=number,
                title=title,
                merged_at=merged_at,
                target=target,
                state=EnumIntegrationState.NOT_RUNNING,
                # The ledger gives no typed gap class. The proof owes a reading.
                gap_class=EnumIntegrationGapClass.NEEDS_READING,
                gap=details.get("gap") or "none stated",
                step=details.get("step") or "none stated",
                read_at=as_of,
                origin=origin,
            )
        )
    return ModelIntegrationReadingBatch(
        as_of=as_of, origin=origin, readings=tuple(readings)
    )
