# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for omnimarket-validate-config CLI.

OMN-10581. Wave 1 / Task 9 of the Public-Shippable plan. Coverage:

- Exit 0 with a minimal valid environment (no services enabled).
- Exit 1 when a service is enabled but its required config is missing.
- Status table is printed to stdout.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from omnimarket.cli.validate_config import main

# All env vars the Settings model reads — cleared so tests are hermetic.
_ALL_SETTINGS_ENV_VARS: tuple[str, ...] = (
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_BROKER",
    "KAFKA_CONSUMER_GROUP",
    "KAFKA_ENVIRONMENT",
    "ENABLE_KAFKA",
    "REDPANDA_ADMIN_HOST",
    "REDPANDA_ADMIN_PORT",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DATABASE",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "OMNIBASE_INFRA_DB_URL",
    "OMNIDASH_ANALYTICS_DB_URL",
    "ENABLE_POSTGRES",
    "QDRANT_HOST",
    "QDRANT_PORT",
    "ENABLE_QDRANT",
    "VALKEY_HOST",
    "VALKEY_PORT",
    "ENABLE_VALKEY",
    "LLM_CODER_URL",
    "LLM_CODER_MODEL_ID",
    "LLM_CODER_FAST_URL",
    "LLM_CODER_FAST_MODEL_ID",
    "LLM_REASONER_URL",
    "LLM_REASONER_MODEL_ID",
    "LLM_EMBEDDING_URL",
    "LLM_EMBEDDING_MODEL_ID",
    "LLM_GLM_URL",
    "LLM_GLM_API_KEY",
    "LLM_GLM_MODEL_NAME",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_PAT",
    "LINEAR_API_KEY",
    "EMBEDDING_MODEL_URL",
    "ENABLE_MEMORY_SERVICE",
    "ONEX_EMIT_SOCKET_PATH",
    "ONEX_EMIT_SPOOL_DIR",
    "ONEX_EMIT_DAEMON_SOCKET",
    "ONEX_STATE_ROOT",
    "ONEX_DASHBOARD_API",
    "ONEX_INFRA_SSH_TARGET",
    "ONEX_TARGET_RUNTIME_ADDRESS",
    "SLACK_BOT_TOKEN",
)


@pytest.mark.unit
def test_validate_config_exits_zero_with_no_services_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With all ENABLE_* flags false (defaults), validate-config exits 0."""
    for var in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    result = runner.invoke(main, catch_exceptions=False)

    assert result.exit_code == 0, (
        f"Expected exit 0, got {result.exit_code}:\n{result.output}"
    )
    assert "OK" in result.output


@pytest.mark.unit
def test_validate_config_exits_one_when_kafka_enabled_without_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 1 when ENABLE_KAFKA=true but no broker is configured."""
    for var in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENABLE_KAFKA", "true")

    runner = CliRunner()
    result = runner.invoke(main, catch_exceptions=False)

    assert result.exit_code == 1, (
        f"Expected exit 1, got {result.exit_code}:\n{result.output}"
    )
    assert "INVALID" in result.output
    assert "KAFKA_BOOTSTRAP_SERVERS" in result.output


@pytest.mark.unit
def test_validate_config_exits_one_when_postgres_enabled_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 1 when ENABLE_POSTGRES=true but no host/port/database/user/password."""
    for var in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENABLE_POSTGRES", "true")

    runner = CliRunner()
    result = runner.invoke(main, catch_exceptions=False)

    assert result.exit_code == 1, (
        f"Expected exit 1, got {result.exit_code}:\n{result.output}"
    )
    assert "INVALID" in result.output
    assert "POSTGRES_HOST" in result.output


@pytest.mark.unit
def test_validate_config_exits_zero_when_postgres_enabled_with_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 0 when ENABLE_POSTGRES=true and a full DSN URL is provided."""
    for var in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENABLE_POSTGRES", "true")
    monkeypatch.setenv("OMNIBASE_INFRA_DB_URL", "postgresql://user:pass@host:5432/db")

    runner = CliRunner()
    result = runner.invoke(main, catch_exceptions=False)

    assert result.exit_code == 0, (
        f"Expected exit 0, got {result.exit_code}:\n{result.output}"
    )
    assert "OK" in result.output


@pytest.mark.unit
def test_validate_config_status_table_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The output contains the status table header row."""
    for var in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()
    result = runner.invoke(main, catch_exceptions=False)

    assert "Field" in result.output
    assert "Source" in result.output
    assert "Value" in result.output
