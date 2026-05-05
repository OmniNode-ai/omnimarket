# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ``omnimarket.config.Settings``.

OMN-10548. Wave 1 / Task 2 of the Public-Shippable plan. Coverage:

- Empty defaults: ``Settings()`` instantiates with no env, no .env, all
  fields zero / empty / false / SecretStr("").
- Per-field absence: each ``enable_*`` flag triggers structured errors when
  its required fields are missing, named by field.
- Default-content regex: the model class definition itself contains no
  forbidden literals (LAN IPs, user paths, private HF model orgs, AWS
  account / SSO role / EC2 ids, personal handles).
- DSN precedence: ``*_db_url`` overrides individual ``POSTGRES_*`` fields.
- Kafka alias: ``kafka_broker`` is accepted when ``kafka_bootstrap_servers``
  is unset.
"""

from __future__ import annotations

import inspect
import re

import pytest
from pydantic import SecretStr

from omnimarket.config.settings import Settings

# ---------------------------------------------------------------------------
# Empty-default invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_construction_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() with no env vars must instantiate cleanly."""
    # Strip every env var the model reads so no .env or shell leaks in.
    for name in (
        "KAFKA_BOOTSTRAP_SERVERS",
        "KAFKA_BROKER",
        "KAFKA_CONSUMER_GROUP",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "OMNIBASE_INFRA_DB_URL",
        "OMNIDASH_ANALYTICS_DB_URL",
        "QDRANT_HOST",
        "QDRANT_PORT",
        "VALKEY_HOST",
        "VALKEY_PORT",
        "LLM_CODER_URL",
        "LLM_CODER_FAST_URL",
        "LLM_REASONER_URL",
        "LLM_EMBEDDING_URL",
        "LLM_GLM_URL",
        "LLM_GLM_API_KEY",
        "EMBEDDING_MODEL_URL",
        "ENABLE_KAFKA",
        "ENABLE_POSTGRES",
        "ENABLE_QDRANT",
        "ENABLE_VALKEY",
        "ENABLE_MEMORY_SERVICE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.kafka_bootstrap_servers == ""
    assert settings.kafka_broker == ""
    assert settings.postgres_host == ""
    assert settings.postgres_port == 0
    assert settings.postgres_password.get_secret_value() == ""
    assert settings.qdrant_host == ""
    assert settings.qdrant_port == 0
    assert settings.llm_coder_url == ""
    assert settings.llm_coder_model_id == ""
    assert settings.llm_glm_api_key.get_secret_value() == ""
    assert settings.enable_kafka is False
    assert settings.enable_postgres is False
    assert settings.enable_qdrant is False
    assert settings.enable_valkey is False
    assert settings.enable_memory_service is False


@pytest.mark.unit
def test_validate_required_services_clean_when_all_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_KAFKA", raising=False)
    monkeypatch.delenv("ENABLE_POSTGRES", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.validate_required_services() == []


# ---------------------------------------------------------------------------
# Per-field absence checks (one test per missing-required-field)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_kafka_required_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.delenv("KAFKA_BROKER", raising=False)
    settings = Settings(_env_file=None, enable_kafka=True)  # type: ignore[call-arg]
    errors = settings.validate_required_services()
    assert len(errors) == 1
    assert "KAFKA_BOOTSTRAP_SERVERS" in errors[0]


@pytest.mark.unit
def test_kafka_satisfied_by_broker_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        enable_kafka=True,
        kafka_broker="broker.example:9092",
    )
    assert settings.validate_required_services() == []


@pytest.mark.unit
def test_postgres_host_required_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "OMNIBASE_INFRA_DB_URL",
        "OMNIDASH_ANALYTICS_DB_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None, enable_postgres=True)  # type: ignore[call-arg]
    errors = settings.validate_required_services()
    assert any("POSTGRES_HOST" in e for e in errors)
    assert any("POSTGRES_PORT" in e for e in errors)
    assert any("POSTGRES_DATABASE" in e for e in errors)
    assert any("POSTGRES_USER" in e for e in errors)
    assert any("POSTGRES_PASSWORD" in e for e in errors)


@pytest.mark.unit
def test_postgres_satisfied_by_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        enable_postgres=True,
        omnibase_infra_db_url=SecretStr(
            "postgresql://u:p@db.example:5432/omnibase_infra"
        ),
    )
    assert settings.validate_required_services() == []


@pytest.mark.unit
def test_qdrant_required_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QDRANT_HOST", raising=False)
    monkeypatch.delenv("QDRANT_PORT", raising=False)
    settings = Settings(_env_file=None, enable_qdrant=True)  # type: ignore[call-arg]
    errors = settings.validate_required_services()
    assert any("QDRANT_HOST" in e for e in errors)
    assert any("QDRANT_PORT" in e for e in errors)


@pytest.mark.unit
def test_valkey_required_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VALKEY_HOST", raising=False)
    monkeypatch.delenv("VALKEY_PORT", raising=False)
    settings = Settings(_env_file=None, enable_valkey=True)  # type: ignore[call-arg]
    errors = settings.validate_required_services()
    assert any("VALKEY_HOST" in e for e in errors)
    assert any("VALKEY_PORT" in e for e in errors)


@pytest.mark.unit
def test_memory_service_required_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL_URL", raising=False)
    settings = Settings(_env_file=None, enable_memory_service=True)  # type: ignore[call-arg]
    errors = settings.validate_required_services()
    assert len(errors) == 1
    assert "EMBEDDING_MODEL_URL" in errors[0]


# ---------------------------------------------------------------------------
# No-forbidden-literals invariant against the model class itself
# ---------------------------------------------------------------------------


# Mirrors plan Task 2 acceptance: regex test against the model definition's
# source so no future contributor can sneak a baked default past code review.
_FORBIDDEN_LITERAL_PATTERN = re.compile(
    r"192\.168\.86\."
    r"|/Users/jonah"
    r"|/Volumes/PRO-G40"
    r"|cyankiwi/"
    r"|Corianas/"
    r"|mlx-community/(Qwen3-Next|DeepSeek|Qwen3-Embedding-8B|Qwen3\.5)"
    r"|jonahgabriel"
    r"|dash\.dev\.omninode\.ai"
    r"|272493677981"
    r"|OmniCloudPlatformAdmin"
    r"|i-0e596e8b557e27785"
    r"|onreviewbot@gmail\.com"
)


@pytest.mark.unit
def test_no_forbidden_literals_in_settings_module() -> None:
    """The Settings module source must contain zero leak literals.

    This is a strictly stronger check than scanning ``Settings()`` instances:
    even a docstring example or commented-out default that mentions a private
    LAN IP / user path / model org would fail.
    """
    import omnimarket.config.settings as mod

    source = inspect.getsource(mod)
    matches = _FORBIDDEN_LITERAL_PATTERN.findall(source)
    assert matches == [], (
        f"settings.py contains forbidden leak literals: {matches}. "
        "All defaults must be empty / zero / false. Move host/path/model "
        "examples to docs/audits/, not into the model class."
    )


@pytest.mark.unit
def test_no_forbidden_literals_in_field_defaults() -> None:
    """The static field defaults on the Settings class must be leak-free.

    Inspecting ``Settings.model_fields`` reads the declared defaults directly
    from the class, bypassing any environment / .env contamination on the
    dev machine. This is what plan Task 2 acceptance asks for: zero forbidden
    literals in any declared default.
    """
    findings: list[str] = []
    for name, info in Settings.model_fields.items():
        default = info.get_default(call_default_factory=True)
        # SecretStr / int / bool / str — repr captures all without exposing secrets
        # since SecretStr.__repr__ is "SecretStr('**********')" or "SecretStr('')".
        rendered = repr(default)
        if _FORBIDDEN_LITERAL_PATTERN.search(rendered):
            findings.append(f"{name}={rendered}")
    assert findings == [], (
        f"Settings has forbidden literals in field defaults: {findings}"
    )


# ---------------------------------------------------------------------------
# get_settings() singleton behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_settings_is_cached() -> None:
    from omnimarket.config.settings import get_settings

    a = get_settings()
    b = get_settings()
    assert a is b
