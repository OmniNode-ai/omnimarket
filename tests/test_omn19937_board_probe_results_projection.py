"""OMN-19937 — durable board-probe-result projection acceptance tests."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.nodes.node_projection_board_probe_results.contract_topics import (
    SUBSCRIBE_TOPICS,
    TOPIC_BOARD_PROBE_RESULT,
    TOPIC_DLQ,
    TOPIC_EXPOSURE,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.board_probe_results_fold import (
    HandlerProjectionBoardProbeResults,
    satisfies_verdict_request,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.handler_board_probe_results_writer import (
    _UPSERT,
    SELECT_LATEST_PER_SUBJECT,
    BoardProbeResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_board_probe_results.models import (
    EnumBoardProbeOutcome,
    EnumBoardProbeRuntimeLane,
    ModelBoardProbeResultEvent,
    ModelBoardProbeResultPayload,
    ModelBoardProbeResultRow,
)
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    load_projection_exposures_from_contract,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner
from omnimarket.projection.table_reader import ProjectionReadError, TableRowSource

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_board_probe_results"
    / "contract.yaml"
)


def _event(
    *,
    outcome: str = "PASS",
    finished_at: str = "2026-09-28T15:00:00Z",
    source_offset: int = 1,
    check_id: str = "branch-protection",
    subject_kind: str = "pull_request",
    subject: str = "OmniNode-ai/omnimarket#321",
    repo: str = "OmniNode-ai/omnimarket",
    sha: str = "a" * 40,
    surface_instance: str = "github-main",
    execution_id: str = "probe-run-1",
) -> ModelBoardProbeResultEvent:
    return ModelBoardProbeResultEvent.model_validate(
        {
            "check_id": check_id,
            "subject_kind": subject_kind,
            "subject": subject,
            "repo": repo,
            "sha": sha,
            "surface_instance": surface_instance,
            "execution_id": execution_id,
            "outcome": outcome,
            "reasons": [] if outcome == "PASS" else ["probe did not pass"],
            "evidence_items": ["probe://board/result"],
            "finished_at": finished_at,
            "source_offset": source_offset,
        }
    )


def _fold_all(
    events: Iterable[ModelBoardProbeResultEvent],
) -> dict[tuple[str, str, str, str, str, str], ModelBoardProbeResultRow]:
    definition = HandlerProjectionBoardProbeResults()
    rows: dict[tuple[str, str, str, str, str, str], ModelBoardProbeResultRow] = {}
    for event in events:
        rows[event.key] = definition.fold(rows.get(event.key), event)
    return rows


def _row_digests(
    rows: dict[tuple[str, str, str, str, str, str], ModelBoardProbeResultRow],
) -> list[str]:
    digests: list[str] = []
    for key in sorted(rows):
        canonical = json.dumps(
            rows[key].model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digests.append(hashlib.sha256(canonical).hexdigest())
    return digests


def test_ac1_two_results_for_the_same_key_converge_on_later_finished_at_and_offset() -> (
    None
):
    definition = HandlerProjectionBoardProbeResults()
    older = _event(
        outcome="FAIL",
        finished_at="2026-09-28T14:00:00Z",
        source_offset=90,
    )
    newer = _event(
        outcome="PASS",
        finished_at="2026-09-28T15:00:00Z",
        source_offset=3,
    )

    for first, second in ((older, newer), (newer, older)):
        row = definition.fold(None, first)
        row = definition.fold(row, second)
        assert row.outcome is EnumBoardProbeOutcome.PASS
        assert row.finished_at.isoformat() == "2026-09-28T15:00:00+00:00"
        assert row.source_offset == 3

    lower_offset = _event(outcome="FAIL", source_offset=10)
    higher_offset = _event(outcome="PASS", source_offset=11)
    for first, second in ((lower_offset, higher_offset), (higher_offset, lower_offset)):
        row = definition.fold(None, first)
        row = definition.fold(row, second)
        assert row.outcome is EnumBoardProbeOutcome.PASS
        assert row.source_offset == 11


def test_ac2_only_a_pass_for_the_same_execution_id_satisfies_a_verdict_request() -> (
    None
):
    requested = (
        "branch-protection",
        "pull_request",
        "OmniNode-ai/omnimarket",
        "a" * 40,
        "github-main",
        "probe-run-requested",
    )
    another_execution = _fold_all(
        [_event(execution_id="probe-run-other", outcome="PASS")]
    ).values()
    assert not satisfies_verdict_request(another_execution, requested)

    for outcome in ("FAIL", "INDETERMINATE"):
        rows = _fold_all(
            [_event(execution_id="probe-run-requested", outcome=outcome)]
        ).values()
        assert not satisfies_verdict_request(rows, requested)

    exact_pass = _fold_all(
        [_event(execution_id="probe-run-requested", outcome="PASS")]
    ).values()
    assert satisfies_verdict_request(exact_pass, requested)


def test_ac3_partition_preserving_replay_produces_byte_identical_row_digests() -> None:
    alpha = "OmniNode-ai/omnimarket#321"
    beta = "OmniNode-ai/omnibase-infra#654"
    fixed = [
        _event(
            subject=alpha,
            source_offset=10,
            outcome="FAIL",
            finished_at="2026-09-28T12:00:00Z",
        ),
        _event(
            subject=beta,
            repo="OmniNode-ai/omnibase_infra",
            sha="b" * 40,
            execution_id="probe-run-2",
            source_offset=20,
            outcome="INDETERMINATE",
            finished_at="2026-09-28T12:30:00Z",
        ),
        _event(
            subject=alpha,
            source_offset=11,
            outcome="PASS",
            finished_at="2026-09-28T13:00:00Z",
        ),
        _event(
            subject=beta,
            repo="OmniNode-ai/omnibase_infra",
            sha="b" * 40,
            execution_id="probe-run-3",
            source_offset=21,
            outcome="PASS",
            finished_at="2026-09-28T13:30:00Z",
        ),
    ]
    shuffled_partition_preserving = [fixed[1], fixed[0], fixed[3], fixed[2]]

    expected = _row_digests(_fold_all(fixed))
    replayed = _row_digests(_fold_all(shuffled_partition_preserving))

    assert replayed == expected


class _RecordingConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Message:
    topic = TOPIC_BOARD_PROBE_RESULT
    partition = 0
    offset = 41
    value = json.dumps(
        {
            "payload": {
                "check_id": "",
                "subject_kind": "pull_request",
                "subject": "OmniNode-ai/omnimarket#321",
                "repo": "OmniNode-ai/omnimarket",
                "sha": "a" * 40,
                "surface_instance": "github-main",
                "execution_id": "probe-run-1",
                "outcome": "PASS",
                "reasons": [],
                "evidence_items": [],
                "finished_at": "2026-09-28T15:00:00Z",
            }
        }
    ).encode()


@pytest.mark.asyncio
async def test_a_malformed_event_is_published_to_the_contract_dlq_before_commit() -> (
    None
):
    writer = BoardProbeResultsProjectionWriter()
    consumer = _RecordingConsumer()
    published: list[tuple[str, bytes]] = []
    writer._consumer = consumer  # type: ignore[assignment]

    async def capture(topic: str, value: bytes) -> None:
        published.append((topic, value))

    writer.publish_dlq = capture  # type: ignore[method-assign]

    await writer._handle_message(_Message())

    assert [topic for topic, _ in published] == [TOPIC_DLQ]
    envelope = json.loads(published[0][1])
    assert envelope["original_message"]["check_id"] == ""
    assert "check_id" in envelope["failure_reason"]
    assert len(consumer.commits) == 1
    assert list(consumer.commits[0].values()) == [42]


def test_runtime_lanes_match_the_sibling_projection_scope_exactly() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    lanes = contract["runtime_lanes"]

    assert lanes == ["compose-dev", "onex-lab", "onex-lab-k3s"]
    assert lanes == [lane.value for lane in EnumBoardProbeRuntimeLane]


def test_contract_topics_and_code_are_consistent_and_contract_derived() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["event_bus"]["subscribe_topics"] == list(SUBSCRIBE_TOPICS)
    assert SUBSCRIBE_TOPICS == (TOPIC_BOARD_PROBE_RESULT,)
    assert contract["event_bus"]["dlq_topics"] == [TOPIC_DLQ]
    assert contract["externally_produced_topics"] == [
        {
            "topic": TOPIC_BOARD_PROBE_RESULT,
            "producer": "omnibase_infra board probe result publisher",
        }
    ]
    assert set(ModelBoardProbeResultPayload.model_fields) == {
        "check_id",
        "subject_kind",
        "subject",
        "repo",
        "sha",
        "surface_instance",
        "execution_id",
        "outcome",
        "reasons",
        "evidence_items",
        "finished_at",
    }
    assert _event().partition_key == _event().subject


def test_projection_api_and_two_class_runtime_shape_are_declared() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    exposure = contract["projection_api"]

    assert exposure["expose"] is True
    assert exposure["table"] == "board_probe_results"
    assert exposure["bus_backed"] is True
    assert exposure["topic"] == TOPIC_EXPOSURE
    assert exposure["key_columns"] == [
        "check_id",
        "subject_kind",
        "repo",
        "sha",
        "surface_instance",
        "execution_id",
    ]
    assert issubclass(BoardProbeResultsProjectionWriter, BaseProjectionRunner)
    assert BoardProbeResultsProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert not hasattr(
        HandlerProjectionBoardProbeResults, "onex_runtime_inprocess_dispatch"
    )
    source = inspect.getsource(HandlerProjectionBoardProbeResults)
    for forbidden in ("_db", "asyncpg", "publish", "INSERT INTO", "datetime.now"):
        assert forbidden not in source


def test_sql_uses_the_declared_key_guard_and_latest_per_subject_order() -> None:
    compact_upsert = " ".join(_UPSERT.split())
    compact_latest = " ".join(SELECT_LATEST_PER_SUBJECT.split())

    assert (
        "ON CONFLICT ( check_id, subject_kind, repo, sha, surface_instance, "
        "execution_id ) DO UPDATE" in compact_upsert
    )
    assert "finished_at < EXCLUDED.finished_at" in compact_upsert
    assert "finished_at = EXCLUDED.finished_at" in compact_upsert
    assert "source_offset < EXCLUDED.source_offset" in compact_upsert
    assert (
        "ORDER BY finished_at DESC, source_offset DESC, execution_id DESC LIMIT 1"
        in compact_latest
    )


@pytest.fixture(scope="module")
def exposure_config() -> ProjectionTableConfig:
    # Contract discovery scans every node; build the map once per module.
    return build_projection_topic_map()[TOPIC_EXPOSURE]


@pytest.fixture
def projection_client(
    monkeypatch: pytest.MonkeyPatch, exposure_config: ProjectionTableConfig
) -> TestClient:
    cfg = exposure_config
    source = TableRowSource()
    rows = [
        {
            **_event(execution_id=f"probe-run-{cursor}").model_dump(mode="json"),
            "projection_cursor": cursor,
        }
        for cursor in (1, 2)
    ]

    async def read_rows(
        config: ProjectionTableConfig, **kwargs: Any
    ) -> list[dict[str, Any]]:
        assert config.topic == TOPIC_EXPOSURE
        assert config.relation_schema == "omninode_internal"
        return rows

    async def latest(config: ProjectionTableConfig, **kwargs: Any) -> datetime:
        return datetime(2026, 9, 28, 15, tzinfo=UTC)

    monkeypatch.setattr(source, "rows", read_rows)
    monkeypatch.setattr(source, "latest_event_at", latest)
    monkeypatch.setitem(
        app.dependency_overrides, get_topic_map, lambda: {cfg.topic: cfg}
    )
    monkeypatch.setitem(app.dependency_overrides, get_row_source, lambda: source)
    return TestClient(app)


@pytest.mark.parametrize("route", ["board_probe_results", TOPIC_EXPOSURE])
def test_ac4_projection_route_reads_execution_rows_and_preserves_cursor_paging(
    projection_client: TestClient, route: str
) -> None:
    response = projection_client.get(f"/projection/{route}?limit=1")
    assert response.status_code == 200
    body = response.json()
    assert body["topic"] == TOPIC_EXPOSURE
    assert body["backing"] == "table"
    assert body["rows"][0]["execution_id"] == "probe-run-1"
    assert body["next_cursor"] == "1"
    next_page = projection_client.get(f"/projection/{route}?limit=1&since=1").json()
    assert next_page["rows"][0]["execution_id"] == "probe-run-2"
    assert next_page["next_cursor"] is None
    assert (
        projection_client.get(f"/projection/{route}?correlation_id=other").status_code
        == 422
    )


def test_projection_alias_preserves_database_failure(
    projection_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = app.dependency_overrides[get_row_source]()

    async def unavailable(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise ProjectionReadError("projection_database_unavailable", "database down")

    monkeypatch.setattr(source, "rows", unavailable)
    response = projection_client.get("/projection/board_probe_results")
    assert response.status_code == 503
    assert response.json()["error"] == "projection_database_unavailable"


def test_projection_alias_collision_refuses_instead_of_reading_another_exposure(
    projection_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic_map = app.dependency_overrides[get_topic_map]()
    cfg = topic_map[TOPIC_EXPOSURE]
    other = cfg.model_copy(update={"topic": "another.exposure"})
    topic_map[other.topic] = other
    monkeypatch.setitem(app.dependency_overrides, get_topic_map, lambda: topic_map)
    response = projection_client.get("/projection/board_probe_results")
    assert response.status_code == 409
    assert response.json()["error"] == "ambiguous_projection_alias"
    assert projection_client.get(f"/projection/{TOPIC_EXPOSURE}").status_code == 200


@pytest.mark.parametrize("aliases", ["board_probe_results", [None], [""], [" spaced "]])
def test_malformed_projection_route_aliases_are_excluded(aliases: Any) -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    contract["projection_api"]["route_aliases"] = aliases
    assert (
        load_projection_exposures_from_contract(
            contract, contract["name"], CONTRACT_PATH
        )
        == ()
    )
