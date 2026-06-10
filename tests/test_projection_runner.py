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
