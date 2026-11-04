# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger seam counter: typed per-window counts and the conservation check.

The rolling ledger file, the work-ledger topics and the database projection are three
places one row must reach. The counter counts each side for a window, emits the counts as
an event, and checks file = projected + refused-with-reason + pending-replay. A file row
that none of those explains fails the check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
import yaml

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.handler_seam_counter import (
    HandlerSeamCounter,
    publish_topic_from_contract,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.seam_sources import (
    InMemoryPendingReplay,
    InMemoryRefusals,
    InMemorySeamEventPublisher,
    InMemorySeamSources,
    LedgerFileSource,
    RedpandaSeamSources,
    build_seam_sources,
    load_seam_counter_overlay,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    EnumSeamLocation,
    EnumSeamVerdict,
    EnumSeamWriterPath,
    ModelSeamCounterRequest,
    ModelSeamRefusal,
    ModelSeamRowRef,
    ModelSeamWindow,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE = "node_work_ledger_seam_counter_compute"
WINDOW = ModelSeamWindow(
    since=datetime(2026, 10, 31, 0, 0, tzinfo=UTC),
    until=datetime(2026, 10, 31, 23, 30, tzinfo=UTC),
)
MARKERS = {"bus": "via=bus:", "relay": "via=relay:"}


def _row(n: int, path: EnumSeamWriterPath = EnumSeamWriterPath.BUS) -> ModelSeamRowRef:
    return ModelSeamRowRef(
        row_id=f"{n:064x}", kind="MSG", lane=f"lane-{n}", writer_path=path
    )


def _request() -> ModelSeamCounterRequest:
    return ModelSeamCounterRequest(correlation_id=uuid4(), window=WINDOW)


def _counter(
    sources: InMemorySeamSources | RedpandaSeamSources,
    *,
    refusals: list[ModelSeamRefusal] | None = None,
    pending: set[str] | None = None,
) -> tuple[HandlerSeamCounter, InMemorySeamEventPublisher]:
    publisher = InMemorySeamEventPublisher()
    handler = HandlerSeamCounter(
        sources=sources,
        refusals=InMemoryRefusals(refusals or []),
        pending=InMemoryPendingReplay(pending or set()),
        publisher=publisher,
        topic=publish_topic_from_contract(),
    )
    return handler, publisher


def _all_seen(rows: list[ModelSeamRowRef]) -> InMemorySeamSources:
    ids = [r.row_id for r in rows]
    return InMemorySeamSources(file=rows, topic=ids, projection=ids)


# --- conservation ---------------------------------------------------------------------


def test_seam_conservation_holds_when_every_file_row_is_accounted_for() -> None:
    rows = [_row(n) for n in range(1, 6)]
    sources = InMemorySeamSources(
        file=rows,
        topic=[rows[0].row_id, rows[1].row_id, rows[2].row_id],
        projection=[rows[0].row_id, rows[1].row_id],
    )
    handler, _ = _counter(
        sources,
        refusals=[ModelSeamRefusal(row_id=rows[2].row_id, reason="guard refused")],
        pending={rows[3].row_id, rows[4].row_id},
    )
    result = handler.handle(_request())
    event = result.event
    assert (event.file_count, event.topic_count, event.projection_count) == (5, 3, 2)
    assert (event.projected_count, event.refused_count, event.pending_count) == (
        2,
        1,
        2,
    )
    assert event.unexplained_count == 0
    assert event.conserved is True
    assert result.verdict is EnumSeamVerdict.PASS
    assert result.unexplained == []
    assert (
        event.file_count
        == event.projected_count + event.refused_count + event.pending_count
    )


def test_seam_conservation_fails_the_probe_on_an_unexplained_row() -> None:
    rows = [_row(1), _row(2, EnumSeamWriterPath.FILE_MODE)]
    sources = InMemorySeamSources(
        file=rows, topic=[rows[0].row_id], projection=[rows[0].row_id]
    )
    handler, _ = _counter(sources)
    result = handler.handle(_request())
    assert result.verdict is EnumSeamVerdict.FAIL
    assert result.event.conserved is False
    assert result.event.unexplained_count == 1
    [gone] = result.unexplained
    assert gone.row == rows[1]
    # The row never reached the topic: the loss sits between the file and the topic.
    assert gone.location is EnumSeamLocation.ABSENT_FROM_TOPIC
    assert result.event.unexplained_row_ids == [rows[1].row_id]


def test_seam_conservation_places_a_topic_row_the_projection_lacks() -> None:
    rows = [_row(1)]
    sources = InMemorySeamSources(file=rows, topic=[rows[0].row_id], projection=[])
    result = _counter(sources)[0].handle(_request())
    assert result.verdict is EnumSeamVerdict.FAIL
    assert result.unexplained[0].location is EnumSeamLocation.ON_TOPIC_NOT_PROJECTED


def test_seam_conservation_counts_a_duplicate_file_row_once() -> None:
    row = _row(1)
    sources = InMemorySeamSources(
        file=[row, row], topic=[row.row_id], projection=[row.row_id]
    )
    event = _counter(sources)[0].handle(_request()).event
    assert event.file_count == 1


def test_seam_conservation_emits_the_counts_as_one_event_on_the_contract_topic() -> (
    None
):
    sources = _all_seen([_row(1)])
    handler, publisher = _counter(sources)
    result = handler.handle(_request())
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes"
            / NODE
            / "contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["publish_topics"] == [publisher.published[0][0]]
    assert [event for _, event in publisher.published] == [result.event]


# --- falsifier: one dropped row --------------------------------------------------------


def test_seam_drop_one_row_makes_the_conservation_check_fail() -> None:
    rows = [_row(n) for n in range(1, 11)]
    healthy = _all_seen(rows)
    handler, _ = _counter(healthy)
    assert (
        handler.handle(_request()).verdict is EnumSeamVerdict.PASS
    )  # positive control

    dropped = rows[4].row_id
    broken = InMemorySeamSources(
        file=rows,
        topic=[r.row_id for r in rows if r.row_id != dropped],
        projection=[r.row_id for r in rows if r.row_id != dropped],
    )
    result = _counter(broken)[0].handle(_request())
    assert result.verdict is EnumSeamVerdict.FAIL
    assert result.event.unexplained_row_ids == [dropped]


# --- in-memory overlay -----------------------------------------------------------------


def test_seam_counter_inmemory_overlay_builds_in_memory_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, {"backend": "inmemory"})
    overlay = load_seam_counter_overlay()
    memory = InMemorySeamSources(file=[_row(1)], topic=[], projection=[])
    sources = build_seam_sources(overlay, inmemory=memory)
    assert sources is memory
    assert _counter(memory)[0].handle(_request()).verdict is EnumSeamVerdict.FAIL


def test_seam_counter_inmemory_overlay_without_in_memory_sources_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, {"backend": "inmemory"})
    with pytest.raises(ValueError, match="inmemory"):
        build_seam_sources(load_seam_counter_overlay())


# --- Redpanda overlay ------------------------------------------------------------------


class _FakeTopicReader:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.asked: list[tuple[str, tuple[str, ...], str]] = []

    def row_ids(self, window: ModelSeamWindow) -> list[str]:
        return self.ids


class _FakeProjection:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def row_ids(self, window: ModelSeamWindow) -> list[str]:
        return self.ids


def _ledger_file(tmp_path: Path) -> tuple[Path, list[ModelSeamRowRef]]:
    lines = [
        "2026-10-31T00:05:00Z | MSG | lane=alpha | via=bus:h200 | bus row",
        "2026-10-31T00:06:00Z | MSG | lane=beta | via=relay:h051 | relay row",
        "2026-10-31T00:07:00Z | HOLD | lane=gamma | file mode row",
        "2026-10-30T23:59:59Z | MSG | lane=old | via=bus:h200 | before the window",
        "2026-10-31T23:31:00Z | MSG | lane=late | via=bus:h200 | after the window",
    ]
    path = tmp_path / "ledger.md"
    path.write_text("# ledger\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    refs = LedgerFileSource(
        lambda: path.read_text(encoding="utf-8"), MARKERS
    ).file_rows(WINDOW)
    return path, list(refs)


def test_seam_counter_redpanda_overlay_reads_the_topic_and_projection_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger, refs = _ledger_file(tmp_path)
    install_node_overlay(
        monkeypatch,
        tmp_path / "overlays",
        NODE,
        {
            "backend": "redpanda",
            "bootstrap_servers": "redpanda.test:9092",
            "consumer_group": "seam-counter-test",
            "topics": ["work-ledger-msg.test", "work-ledger-hold.test"],
            "ledger_file": str(ledger),
            "writer_path_markers": MARKERS,
        },
    )
    overlay = load_seam_counter_overlay()
    ids = [r.row_id for r in refs]
    seen: list[tuple[str, tuple[str, ...], str]] = []

    def topic_reader_factory(
        bootstrap: str, topics: tuple[str, ...], group: str
    ) -> _FakeTopicReader:
        seen.append((bootstrap, topics, group))
        return _FakeTopicReader(ids[:2])

    sources = build_seam_sources(
        overlay,
        topic_reader_factory=topic_reader_factory,
        projection=_FakeProjection(ids[:1]),
        ledger_text_reader=lambda name: Path(name).read_text(encoding="utf-8"),
    )
    assert isinstance(sources, RedpandaSeamSources)
    assert seen == [
        (
            "redpanda.test:9092",
            ("work-ledger-msg.test", "work-ledger-hold.test"),
            "seam-counter-test",
        )
    ]
    # The window cuts the file side to the three rows inside it, with their writer path.
    assert [(r.lane, r.kind, r.writer_path) for r in refs] == [
        ("alpha", "MSG", EnumSeamWriterPath.BUS),
        ("beta", "MSG", EnumSeamWriterPath.RELAY),
        ("gamma", "HOLD", EnumSeamWriterPath.FILE_MODE),
    ]
    result = _counter(sources)[0].handle(_request())
    assert (
        result.event.file_count,
        result.event.topic_count,
        result.event.projection_count,
    ) == (3, 2, 1)
    # beta is on the topic and not projected; gamma never reached the topic.
    by_lane = {u.row.lane: u.location for u in result.unexplained}
    assert by_lane == {
        "beta": EnumSeamLocation.ON_TOPIC_NOT_PROJECTED,
        "gamma": EnumSeamLocation.ABSENT_FROM_TOPIC,
    }
    assert result.verdict is EnumSeamVerdict.FAIL


def test_seam_counter_redpanda_overlay_missing_the_broker_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE,
        {"backend": "redpanda", "topics": ["t.test"], "ledger_file": "l.md"},
    )
    with pytest.raises(ValueError, match="bootstrap_servers"):
        load_seam_counter_overlay()


def test_seam_counter_overlay_unknown_backend_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE, {"backend": "carrier-pigeon"})
    with pytest.raises(ValueError, match="backend"):
        load_seam_counter_overlay()


# --- boot-resolvable construction -----------------------------------------------------


class _FakeContainer:
    """Answers get_service by protocol, as the runtime container does."""

    def __init__(self, services: dict[type, object]) -> None:
        self._services = services

    def get_service(self, key: type) -> object:
        return self._services[key]


def test_seam_counter_constructs_from_the_container_alone_and_resolves_its_sources() -> (
    None
):
    from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.protocols import (
        ProtocolPendingReplaySource,
        ProtocolRefusalSource,
        ProtocolSeamEventPublisher,
        ProtocolSeamSources,
    )

    rows = [_row(1), _row(2)]
    publisher = InMemorySeamEventPublisher()
    container = _FakeContainer(
        {
            ProtocolSeamSources: _all_seen(rows),
            ProtocolRefusalSource: InMemoryRefusals([]),
            ProtocolPendingReplaySource: InMemoryPendingReplay(set()),
            ProtocolSeamEventPublisher: publisher,
        }
    )
    handler = HandlerSeamCounter(cast("ModelONEXContainer", container))
    result = handler.handle(_request())
    assert result.verdict is EnumSeamVerdict.PASS
    assert result.event.file_count == 2
    assert [topic for topic, _ in publisher.published] == [
        publish_topic_from_contract()
    ]


def test_seam_counter_without_a_source_or_container_fails_by_name() -> None:
    handler = HandlerSeamCounter()
    with pytest.raises(ValueError, match="ProtocolSeamSources"):
        handler.handle(_request())
