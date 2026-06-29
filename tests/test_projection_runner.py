"""Tests for BaseProjectionRunner helper functions."""

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.projection.runner import (
    PROJECTION_RUNTIME_BINDING_OVERLAY_ENV,
    BaseProjectionRunner,
    MessageMeta,
    ModelProjectionRuntimeBinding,
    coalesce,
    deterministic_correlation_id,
    load_projection_runtime_binding_overlay,
    safe_float,
    safe_int,
    safe_parse_date,
)


class DummyProjectionRunner(BaseProjectionRunner):
    @property
    def topics(self) -> list[str]:
        return ["onex.snapshot.test.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        return True


class TestDeterministicCorrelationId:
    def test_format(self) -> None:
        result = deterministic_correlation_id("topic", 0, 42)
        # UUID-shaped: 8-4-4-4-12 hex
        parts = result.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_deterministic(self) -> None:
        a = deterministic_correlation_id("topic", 0, 100)
        b = deterministic_correlation_id("topic", 0, 100)
        assert a == b

    def test_different_inputs(self) -> None:
        a = deterministic_correlation_id("topic", 0, 100)
        b = deterministic_correlation_id("topic", 0, 101)
        assert a != b


class TestSafeParseDate:
    def test_iso_string(self) -> None:
        result = safe_parse_date("2026-04-06T12:00:00Z")
        assert isinstance(result, datetime)

    def test_none(self) -> None:
        result = safe_parse_date(None)
        assert isinstance(result, datetime)
        # Should be approximately now
        delta = abs((datetime.now(UTC) - result).total_seconds())
        assert delta < 5

    def test_empty_string(self) -> None:
        result = safe_parse_date("")
        assert isinstance(result, datetime)

    def test_datetime_passthrough(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        result = safe_parse_date(dt)
        assert result == dt

    def test_malformed(self) -> None:
        result = safe_parse_date("not-a-date")
        assert isinstance(result, datetime)


class TestSafeFloat:
    def test_valid(self) -> None:
        assert safe_float("3.14") == 3.14

    def test_none(self) -> None:
        assert safe_float(None) == 0.0

    def test_invalid(self) -> None:
        assert safe_float("abc") == 0.0

    def test_nan(self) -> None:
        assert safe_float(float("nan")) == 0.0


class TestSafeInt:
    def test_valid(self) -> None:
        assert safe_int("42") == 42

    def test_none(self) -> None:
        assert safe_int(None) == 0

    def test_invalid(self) -> None:
        assert safe_int("abc") == 0


class TestCoalesce:
    def test_first_truthy(self) -> None:
        assert coalesce(None, "", "hello") == "hello"

    def test_all_falsy(self) -> None:
        assert coalesce(None, "", 0) == 0

    def test_empty(self) -> None:
        assert coalesce() is None


class TestProjectionRuntimeBinding:
    def test_explicit_binding_constructs_runner_with_env_unset(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("KAFKA_BROKERS", raising=False)
        monkeypatch.delenv("KAFKA_CONSUMER_GROUP", raising=False)
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)

        binding = ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="redpanda.internal:9092",
            kafka_consumer_group="projection-contract-group",
            kafka_client_id="projection-contract-client",
            database_url="postgresql://projection:secret@db.internal:5432/projections",
        )

        runner = DummyProjectionRunner(runtime_binding=binding)

        assert runner.kafka_bootstrap_servers == "redpanda.internal:9092"
        assert runner.runtime_binding_source == "explicit"
        assert runner._group_id == "projection-contract-group"
        assert runner._client_id == "projection-contract-client"
        assert (
            runner.db._dsn
            == "postgresql://projection:secret@db.internal:5432/projections"
        )

    def test_loads_projection_runtime_binding_overlay(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("KAFKA_BROKERS", raising=False)
        monkeypatch.delenv("KAFKA_CONSUMER_GROUP", raising=False)
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        overlay = tmp_path / "projection-runtime-binding.yaml"
        overlay.write_text(
            "\n".join(
                [
                    "kafka_bootstrap_servers: redpanda.contract:9092",
                    "kafka_consumer_group: projection-overlay-group",
                    "kafka_client_id: projection-overlay-client",
                    "database_url: postgresql://projection:secret@db.contract:5432/projections",
                ]
            ),
            encoding="utf-8",
        )

        binding = load_projection_runtime_binding_overlay(overlay)
        runner = DummyProjectionRunner(runtime_binding=binding)

        assert runner.kafka_bootstrap_servers == "redpanda.contract:9092"
        assert runner.runtime_binding_source == f"overlay:{overlay}"
        assert (
            runner.db._dsn
            == "postgresql://projection:secret@db.contract:5432/projections"
        )

    def test_overlay_selector_constructs_runner_with_env_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("KAFKA_BROKERS", raising=False)
        monkeypatch.delenv("KAFKA_CONSUMER_GROUP", raising=False)
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        overlay = tmp_path / "projection-runtime-binding.yaml"
        overlay.write_text(
            "\n".join(
                [
                    "kafka_bootstrap_servers: redpanda.selector:9092",
                    "database_url: postgresql://projection:secret@db.selector:5432/projections",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, str(overlay))

        runner = DummyProjectionRunner()

        assert runner.kafka_bootstrap_servers == "redpanda.selector:9092"
        assert runner.runtime_binding_source == f"overlay:{overlay}"
        assert (
            runner.db._dsn
            == "postgresql://projection:secret@db.selector:5432/projections"
        )

    def test_secret_ref_uses_isolated_env_secret_boundary(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "PROJECTION_DATABASE_URL_SECRET",
            "postgresql://projection:secret@db.secret:5432/projections",
        )
        binding = ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="redpanda.secret:9092",
            database_url_secret_ref="env:PROJECTION_DATABASE_URL_SECRET",
        )

        assert (
            binding.resolve_database_url()
            == "postgresql://projection:secret@db.secret:5432/projections"
        )

    def test_unresolved_secret_ref_fails(self, monkeypatch) -> None:
        monkeypatch.delenv("MISSING_PROJECTION_DATABASE_URL", raising=False)
        binding = ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="redpanda.secret:9092",
            database_url_secret_ref="env:MISSING_PROJECTION_DATABASE_URL",
        )

        with pytest.raises(RuntimeError, match="unresolved"):
            binding.resolve_database_url()


class _RecordingConsumer:
    """Minimal AIOKafkaConsumer stand-in that records explicit offset commits."""

    def __init__(self) -> None:
        self.commits: list[dict[Any, int]] = []

    async def commit(self, offsets: dict[Any, int]) -> None:
        self.commits.append(offsets)


class _Msg:
    """Minimal Kafka message stand-in for _handle_message."""

    def __init__(
        self, *, topic: str, partition: int, offset: int, value: bytes | None
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class _EnvelopeFreeRunner(BaseProjectionRunner):
    """Runner whose project_event outcome is controlled per test.

    OMN-13350 fail-loud: drives _handle_message directly with a recording
    consumer to prove the offset is committed ONLY when a message is fully
    accounted for, and that a projection error propagates WITHOUT committing.
    """

    def __init__(self, *, behavior: str) -> None:
        binding = ModelProjectionRuntimeBinding(
            kafka_bootstrap_servers="redpanda.test:9092",
            database_url="postgresql://p:s@db.test:5432/projections",
        )
        super().__init__(runtime_binding=binding)
        self._behavior = behavior
        self.project_event_calls = 0
        self._consumer = _RecordingConsumer()  # type: ignore[assignment]

    @property
    def topics(self) -> list[str]:
        return ["onex.evt.omnimarket.node-generation-completed.v1"]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        self.project_event_calls += 1
        if self._behavior == "raise":
            # Mirror the live UndefinedColumn the projection raises on a
            # schema-drifted generation_events table.
            raise RuntimeError(
                'column "corpus_checked" of relation "generation_events" does not exist'
            )
        if self._behavior == "unhandled":
            return False
        return True

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        # No real DB in this unit test; the watermark write is exercised
        # elsewhere and is not the offset-commit path under test here.
        return None


def _msg() -> _Msg:
    # Non-None, unwrappable value so project_event is reached.
    return _Msg(
        topic="onex.evt.omnimarket.node-generation-completed.v1",
        partition=0,
        offset=41,
        value=b'{"payload": {"correlation_id": "c-1"}}',
    )


class TestFailLoudOffsetCommit:
    """OMN-13350: no commit-and-drop. Offset advances only when accounted for."""

    @pytest.mark.asyncio
    async def test_successful_projection_commits_offset(self) -> None:
        runner = _EnvelopeFreeRunner(behavior="project")
        await runner._handle_message(_msg())
        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert len(commits) == 1, "a projected message must commit its offset"
        # Committed position is offset + 1 (next message to read).
        assert list(commits[0].values()) == [42]
        assert runner.stats.events_projected == 1
        assert runner.stats.errors_count == 0

    @pytest.mark.asyncio
    async def test_projection_error_does_not_commit_and_raises(self) -> None:
        runner = _EnvelopeFreeRunner(behavior="raise")
        with pytest.raises(RuntimeError, match="does not exist"):
            await runner._handle_message(_msg())
        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert commits == [], (
            "a failed projection must NOT commit the offset — committing here is "
            "the silent-drop data-loss bug (the message would be skipped while "
            "the consumer group reported Stable)"
        )
        assert runner.stats.errors_count == 1
        assert runner.stats.events_projected == 0

    @pytest.mark.asyncio
    async def test_unhandled_topic_commits_to_avoid_partition_wedge(self) -> None:
        # project_event returning False means "not my topic" — a handled
        # non-event. It must commit so the partition is not wedged forever on a
        # message this runner does not project.
        runner = _EnvelopeFreeRunner(behavior="unhandled")
        await runner._handle_message(_msg())
        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert len(commits) == 1
        assert runner.stats.events_projected == 0
        assert runner.stats.errors_count == 0

    @pytest.mark.asyncio
    async def test_empty_value_commits_as_non_event(self) -> None:
        runner = _EnvelopeFreeRunner(behavior="project")
        msg = _Msg(
            topic="onex.evt.omnimarket.node-generation-completed.v1",
            partition=0,
            offset=7,
            value=None,
        )
        await runner._handle_message(msg)
        commits = runner._consumer.commits  # type: ignore[attr-defined]
        assert len(commits) == 1, "an empty message is a non-event; commit and move on"
        assert runner.project_event_calls == 0

    @pytest.mark.asyncio
    async def test_auto_commit_is_disabled(self) -> None:
        """The consumer is constructed with enable_auto_commit disabled.

        Auto-commit was the data-loss mechanism: the timer advanced the offset
        regardless of DB-write success. Guard the source so a future edit cannot
        silently re-enable it.
        """
        import inspect

        from omnimarket.projection import runner as runner_module

        source = inspect.getsource(runner_module.BaseProjectionRunner.run)
        assert "enable_auto_commit=False" in source
        assert "enable_auto_commit=True" not in source
