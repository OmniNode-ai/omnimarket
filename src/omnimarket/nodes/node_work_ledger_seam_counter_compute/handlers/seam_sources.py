# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam sources: in-memory fakes, the ledger-file reader, and the overlay-driven builder.

The overlay (``<root>/node_work_ledger_seam_counter_compute/overlay.yaml``) names the
backend. ``inmemory`` takes caller-supplied in-memory sources. ``redpanda`` reads the
ledger file for the file side and takes a topic reader and a projection reader built
from the overlay's broker, topics and consumer group by factories the caller injects, so
no broker client is constructed here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.work_ledger_text import ledger_row_stamp, split_ledger_rows
from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.protocols import (
    ProtocolRowIdReader,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    EnumSeamWriterPath,
    ModelSeamCountedEvent,
    ModelSeamCounterOverlay,
    ModelSeamRefusal,
    ModelSeamRowRef,
    ModelSeamWindow,
)

OVERLAY_ID = "node_work_ledger_seam_counter_compute"

TopicReaderFactory = Callable[[str, tuple[str, ...], str], ProtocolRowIdReader]
LedgerTextReader = Callable[[str], str]


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.split("\n", 1)[0].split(" | ")]


def _cell_value(cells: Sequence[str], key: str) -> str:
    prefix = f"{key}="
    for cell in cells:
        if cell.startswith(prefix):
            return cell[len(prefix) :]
    return ""


class LedgerFileSource:
    """The file side: rows of the markdown ledger inside a window.

    A row's writer path is the first overlay marker found among its cells (``bus`` or
    ``relay``); a row carrying neither was appended in file mode.
    """

    def __init__(
        self, read_ledger: Callable[[], str], writer_path_markers: dict[str, str]
    ) -> None:
        self._read_ledger = read_ledger
        self._markers = writer_path_markers

    def _writer_path(self, cells: Sequence[str]) -> EnumSeamWriterPath:
        for name in (EnumSeamWriterPath.BUS, EnumSeamWriterPath.RELAY):
            marker = self._markers.get(name.value)
            if marker and any(cell.startswith(marker) for cell in cells):
                return name
        return EnumSeamWriterPath.FILE_MODE

    def file_rows(self, window: ModelSeamWindow) -> list[ModelSeamRowRef]:
        refs: list[ModelSeamRowRef] = []
        for row in split_ledger_rows(self._read_ledger()):
            if not window.since <= ledger_row_stamp(row) <= window.until:
                continue
            cells = _cells(row)
            refs.append(
                ModelSeamRowRef(
                    row_id=work_ledger_row_id(row),
                    kind=cells[1] if len(cells) > 1 else "",
                    lane=_cell_value(cells, "lane") or _cell_value(cells, "from"),
                    writer_path=self._writer_path(cells),
                )
            )
        return refs


class InMemorySeamSources:
    """All three sides held in memory (window-independent)."""

    def __init__(
        self,
        *,
        file: Sequence[ModelSeamRowRef],
        topic: Sequence[str],
        projection: Sequence[str],
    ) -> None:
        self._file = list(file)
        self._topic = list(topic)
        self._projection = list(projection)

    def file_rows(self, window: ModelSeamWindow) -> list[ModelSeamRowRef]:
        return list(self._file)

    def topic_row_ids(self, window: ModelSeamWindow) -> list[str]:
        return list(self._topic)

    def projection_row_ids(self, window: ModelSeamWindow) -> list[str]:
        return list(self._projection)


class RedpandaSeamSources:
    """File side from the ledger file; topic and projection sides from injected readers."""

    def __init__(
        self,
        *,
        file_source: LedgerFileSource,
        topic_reader: ProtocolRowIdReader,
        projection_reader: ProtocolRowIdReader,
    ) -> None:
        self._file = file_source
        self._topic = topic_reader
        self._projection = projection_reader

    def file_rows(self, window: ModelSeamWindow) -> Sequence[ModelSeamRowRef]:
        return self._file.file_rows(window)

    def topic_row_ids(self, window: ModelSeamWindow) -> Sequence[str]:
        return self._topic.row_ids(window)

    def projection_row_ids(self, window: ModelSeamWindow) -> Sequence[str]:
        return self._projection.row_ids(window)


class InMemoryRefusals:
    def __init__(self, refusals: Sequence[ModelSeamRefusal]) -> None:
        self._refusals = list(refusals)

    def refusals(self, window: ModelSeamWindow) -> list[ModelSeamRefusal]:
        return list(self._refusals)


class InMemoryPendingReplay:
    def __init__(self, row_ids: set[str]) -> None:
        self._row_ids = set(row_ids)

    def pending_row_ids(self) -> set[str]:
        return set(self._row_ids)


class InMemorySeamEventPublisher:
    """Records the count events it is given, with their topic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, ModelSeamCountedEvent]] = []

    def publish(self, topic: str, event: ModelSeamCountedEvent) -> None:
        self.published.append((topic, event))


# The overlay fields each backend needs, so a missing one is refused when the overlay
# loads. The backend name selects a row here; no code path compares it with a literal.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "inmemory": (),
    "redpanda": ("bootstrap_servers", "consumer_group", "ledger_file", "topics"),
}


def load_seam_counter_overlay(
    *, explicit: object | None = None
) -> ModelSeamCounterOverlay:
    """Read the overlay; a missing or invalid one refuses with the fields it lacks."""
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelSeamCounterOverlay, explicit=explicit
        )
    except NodeOverlayError as exc:
        raise ValueError(str(exc)) from None
    missing = [
        name for name in _REQUIRED_FIELDS[overlay.backend] if not getattr(overlay, name)
    ]
    if missing:
        raise ValueError(f"backend {overlay.backend} needs overlay fields {missing}")
    return overlay


class SeamSourceParts:
    """The injected parts a backend may assemble its sources from."""

    def __init__(
        self,
        *,
        inmemory: InMemorySeamSources | None,
        topic_reader_factory: TopicReaderFactory | None,
        projection: ProtocolRowIdReader | None,
        ledger_text_reader: LedgerTextReader | None,
    ) -> None:
        self.inmemory = inmemory
        self.topic_reader_factory = topic_reader_factory
        self.projection = projection
        self.ledger_text_reader = ledger_text_reader


def _inmemory_sources(
    overlay: ModelSeamCounterOverlay, parts: SeamSourceParts
) -> InMemorySeamSources | RedpandaSeamSources:
    if parts.inmemory is None:
        raise ValueError("backend inmemory needs the in-memory sources injected")
    return parts.inmemory


def _redpanda_sources(
    overlay: ModelSeamCounterOverlay, parts: SeamSourceParts
) -> InMemorySeamSources | RedpandaSeamSources:
    factory, projection, reader = (
        parts.topic_reader_factory,
        parts.projection,
        parts.ledger_text_reader,
    )
    if factory is None or projection is None or reader is None:
        raise ValueError(
            "backend redpanda needs a topic reader factory, a projection reader "
            "and a ledger text reader"
        )
    if not (
        overlay.bootstrap_servers and overlay.consumer_group and overlay.ledger_file
    ):
        raise ValueError(
            "backend redpanda needs bootstrap_servers, consumer_group and ledger_file"
        )
    ledger_file = overlay.ledger_file
    return RedpandaSeamSources(
        file_source=LedgerFileSource(
            lambda: reader(ledger_file), overlay.writer_path_markers
        ),
        topic_reader=factory(
            overlay.bootstrap_servers, tuple(overlay.topics), overlay.consumer_group
        ),
        projection_reader=projection,
    )


_SOURCE_BUILDERS: dict[
    str,
    Callable[
        [ModelSeamCounterOverlay, SeamSourceParts],
        InMemorySeamSources | RedpandaSeamSources,
    ],
] = {"inmemory": _inmemory_sources, "redpanda": _redpanda_sources}


def build_seam_sources(
    overlay: ModelSeamCounterOverlay,
    *,
    inmemory: InMemorySeamSources | None = None,
    topic_reader_factory: TopicReaderFactory | None = None,
    projection: ProtocolRowIdReader | None = None,
    ledger_text_reader: LedgerTextReader | None = None,
) -> InMemorySeamSources | RedpandaSeamSources:
    """Build the sources the overlay's backend names from the injected parts."""
    parts = SeamSourceParts(
        inmemory=inmemory,
        topic_reader_factory=topic_reader_factory,
        projection=projection,
        ledger_text_reader=ledger_text_reader,
    )
    return _SOURCE_BUILDERS[overlay.backend](overlay, parts)


__all__ = [
    "OVERLAY_ID",
    "InMemoryPendingReplay",
    "InMemoryRefusals",
    "InMemorySeamEventPublisher",
    "InMemorySeamSources",
    "LedgerFileSource",
    "RedpandaSeamSources",
    "build_seam_sources",
    "load_seam_counter_overlay",
]
