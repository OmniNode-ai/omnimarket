# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Render the human-readable work-ledger view from ``work_events`` rows.

[OMN-16180] The generated L1 view. ``docs/tracking/ROLLING_WORK_LEDGER.md``
remains the authoritative L0 record and is still appended under a lock by
``scripts/ledger_lock.py``; this module renders a SECOND, generated view from
the projection so the same narrative can be read without hand-typing it. Nothing
here retires L0 -- that cutover is OMN-16183 (C7).

Everything in this module is PURE. It performs no database I/O: rows arrive as
typed models, and the caller is responsible for obtaining them through the
runtime's contract-declared projection surface. A renderer that opened its own
connection would violate the runtime-owns-the-database boundary
(``feedback_only_runtime_touches_database``) for no benefit.

``render_ledger_view`` and ``parse_ledger_view`` are exact inverses over the
fields they carry, which is what makes OMN-16180 acceptance 5 ("regenerated
markdown ... round-trips the structured fields") mechanically testable rather
than eyeballed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    EnumActorKind,
    ModelWorkEventRow,
)

_HEADER = "| emitted_at (UTC) | kind | summary |"
_DIVIDER = "|---|---|---|"

# A literal pipe would break the table row into extra cells and make the parse
# lossy, so it is escaped on render and restored on parse. Newlines are folded
# for the same reason -- a summary is one ledger LINE by construction.
_PIPE = "|"
_ESCAPED_PIPE = "&#124;"


def _escape_cell(value: str) -> str:
    return value.replace(_PIPE, _ESCAPED_PIPE).replace("\n", " ").replace("\r", " ")


def _unescape_cell(value: str) -> str:
    return value.replace(_ESCAPED_PIPE, _PIPE)


def _format_timestamp(value: datetime) -> str:
    """Render to whole seconds, in UTC, for a value labelled ``Z``.

    ``astimezone(UTC)`` is load-bearing and was a real defect when it read
    ``astimezone(tz=None)``: that converts to the RENDERING MACHINE's local
    zone while the surrounding template still appends ``Z``, so a row stored
    ``2026-08-30T02:09:13+00`` rendered as ``2026-08-29T22:09:13Z`` on an
    EDT host -- off by four hours, on the wrong DAY, and labelled UTC. Caught
    by comparing the generated view against the source rows in Postgres, not
    by a unit test, which is why ``test_timestamps_render_in_utc_not_local``
    now exists.

    A naive datetime is treated as already-UTC rather than silently taking the
    host zone, for the same reason.
    """
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def render_ledger_view(
    rows: Sequence[ModelWorkEventRow],
    *,
    generated_at: datetime | None = None,
) -> str:
    """Render work-event rows as the grouped, human-readable ledger view.

    Rows are grouped by actor and, within an actor, sorted by ``emitted_at``
    descending. That sort is a DISPLAY ordering only and the rendered banner
    says so -- per-actor total order comes from the partition offset, and no
    cross-actor global ordering is claimed at all (OMN-16177's ordering
    contract, deterministic-truth doctrine section 4). Stating it in the
    artifact matters because a reader of a time-sorted table will otherwise
    assume the times ARE the order.

    Args:
        rows: Work-event rows to render.
        generated_at: Stamped into the banner. Omitted entirely when ``None``,
            which keeps the output byte-stable for tests -- a renderer that
            silently substituted ``now()`` could not be compared against a
            golden file.
    """
    lines: list[str] = [
        "# Work ledger -- generated view (L1)",
        "",
        "> GENERATED from `omninode_internal.work_events` by "
        "`node_projection_work_events` (OMN-16180). Do not hand-edit: "
        "regenerate it.",
        ">",
        "> The authoritative L0 record remains "
        "`docs/tracking/ROLLING_WORK_LEDGER.md`, still appended under "
        "`scripts/ledger_lock.py`. L0 and L1 dual-write during the OMN-16176 "
        "transition; retiring L0 is OMN-16183 (C7).",
        ">",
        "> Rows are sorted by `emitted_at` descending. That is a DISPLAY SORT "
        "ONLY -- it is not a claimed global ordering of work.",
        "",
    ]

    if not rows:
        lines.extend(
            [
                "_No work events in the projection for this query._",
                "",
                "An empty view means the SELECT returned nothing -- it is not "
                "evidence that no work happened. Check the projection is "
                "attached and writing before reading this as a fact about the "
                "world.",
                "",
            ]
        )
        return "\n".join(lines)

    by_actor: dict[tuple[EnumActorKind, str], list[ModelWorkEventRow]] = defaultdict(
        list
    )
    for row in rows:
        by_actor[(row.actor_kind, row.actor_id)].append(row)

    ordered_actors = sorted(
        by_actor.items(),
        key=lambda item: max(row.emitted_at for row in item[1]),
        reverse=True,
    )

    total_rows = len(rows)
    lines.append(
        f"**{total_rows} event(s)** across **{len(ordered_actors)} actor(s)**."
        + (
            f" Generated {_format_timestamp(generated_at)}Z."
            if generated_at is not None
            else ""
        )
    )
    lines.append("")

    for (actor_kind, actor_id), actor_rows in ordered_actors:
        actor_rows.sort(key=lambda row: row.emitted_at, reverse=True)
        first = min(row.emitted_at for row in actor_rows)
        last = max(row.emitted_at for row in actor_rows)
        lines.append(f"## {actor_kind.value}: `{actor_id}`")
        lines.append("")
        lines.append(
            f"{len(actor_rows)} event(s), "
            f"{_format_timestamp(first)}Z -> {_format_timestamp(last)}Z"
        )
        lines.append("")
        lines.append(_HEADER)
        lines.append(_DIVIDER)
        for row in actor_rows:
            lines.append(
                f"| {_format_timestamp(row.emitted_at)}Z "
                f"| {_escape_cell(row.event_kind)} "
                f"| {_escape_cell(row.summary)} |"
            )
        lines.append("")

    return "\n".join(lines)


def parse_ledger_view(text: str) -> list[tuple[EnumActorKind, str, str, str, str]]:
    """Parse a rendered view back into ``(actor_kind, actor_id, ts, kind, summary)``.

    The exact inverse of :func:`render_ledger_view` over the fields the table
    carries. This exists so the round-trip is asserted mechanically -- a
    renderer is only trustworthy as a record if what it wrote can be read back
    and compared to the source.
    """
    parsed: list[tuple[EnumActorKind, str, str, str, str]] = []
    actor_kind: EnumActorKind | None = None
    actor_id: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            heading = line.removeprefix("## ")
            kind_text, _, id_text = heading.partition(": ")
            actor_kind = EnumActorKind(kind_text.strip())
            actor_id = id_text.strip().strip("`")
            continue
        if not line.startswith(_PIPE) or line in {_HEADER, _DIVIDER}:
            continue
        cells = [cell.strip() for cell in line.strip(_PIPE).split(_PIPE)]
        if len(cells) != 3:
            continue
        if actor_kind is None or actor_id is None:
            raise ValueError("table row encountered before any actor heading")
        timestamp, kind, summary = cells
        parsed.append(
            (
                actor_kind,
                actor_id,
                timestamp.removesuffix("Z"),
                _unescape_cell(kind),
                _unescape_cell(summary),
            )
        )
    return parsed


def rows_from_records(records: Iterable[dict[str, object]]) -> list[ModelWorkEventRow]:
    """Validate raw ``work_events`` records (e.g. ``json_agg`` output) into rows.

    Kept here rather than in the caller so the view has exactly one typed entry
    point and no path renders un-validated dictionaries.
    """
    return [ModelWorkEventRow.model_validate(record) for record in records]


__all__: list[str] = [
    "parse_ledger_view",
    "render_ledger_view",
    "rows_from_records",
]
