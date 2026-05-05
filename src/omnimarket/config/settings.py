# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket Settings — typed config with empty defaults and per-service flags.

OMN-10548. Wave 1 / Task 2 of the Public-Shippable plan
(`docs/plans/2026-05-05-omnimarket-public-shippable.md`).

Pattern reference: `omniclaude/src/omniclaude/config/settings.py:86-705`. Key
invariants this module enforces:

1. **No baked defaults.** All fields default to empty string, ``None``, ``0``,
   or ``False``. The model class itself contains no LAN IPs, user paths,
   private HF model orgs, AWS account IDs, or other identity literals. A
   class-level regex test
   (`tests/unit/config/test_settings.py::test_no_forbidden_literals_in_defaults`)
   asserts this against the model definition itself, not just instances.
2. **Per-service ``enable_*`` flags.** Required keys are only required when
   their service is enabled (``enable_kafka``, ``enable_postgres``,
   ``enable_valkey``, ``enable_memory_service``).
3. **Structured fail-fast validation.** ``validate_required_services()``
   returns a list of named errors; one error per missing required field.
   Empty list = valid.
4. **No silent fallback.** Where omniclaude uses ``omniclaude_db_url`` as a
   one-DSN-or-fields convenience, omnimarket follows the same shape but does
   not ship per-machine DSN samples.

Field set is derived directly from the OMN-10547 audit CSV
(`docs/audits/2026-05-05-raw-env-usage.csv`) — every required key in the audit
that maps to a production service appears here. Bootstrap-only keys
(``OMNI_HOME``, ``ONEX_STATE_DIR``) and pass-through keys (``model_id_env``,
``url_env``, ``env_var``, etc. — these are dynamic-key plumbing that names an
env var at runtime) are NOT promoted to fields; they remain raw env reads
flagged in Task 3 with ticketed annotations.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def load_env_from_parents() -> None:
    """Walk up from this file looking for a ``.env`` and load it if present.

    Local bootstrap code may call this before constructing ``Settings``.
    Loading is best-effort and never raises —
    in production the env is provided by the runtime, not by a checked-in
    .env file. .env support exists for local dev only.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv not installed → noop
        return

    current = Path(__file__).resolve().parent
    for _ in range(10):
        env_file = current / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return
        parent = current.parent
        if parent == current:
            break
        current = parent


class Settings(BaseSettings):
    """Typed configuration for omnimarket production code.

    Read from environment variables and pydantic's configured ``.env`` file.
    Parent-directory ``.env`` discovery is opt-in through
    ``load_env_from_parents()``. All defaults are empty / zero / false — fields
    are required only when their service is enabled. Use
    ``validate_required_services()`` after construction to get a structured
    list of missing-required errors before starting a service.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # =========================================================================
    # KAFKA / REDPANDA
    # =========================================================================
    kafka_bootstrap_servers: str = Field(
        default="",
        description=(
            "Kafka broker addresses (comma-separated host:port pairs). "
            "REQUIRED when ENABLE_KAFKA=true. No default to prevent silent "
            "localhost connections."
        ),
    )
    kafka_broker: str = Field(
        default="",
        description=(
            "Single-broker convenience alias for kafka_bootstrap_servers. "
            "When unset, Settings reads kafka_bootstrap_servers."
        ),
    )
    kafka_consumer_group: str = Field(
        default="",
        description="Kafka consumer group ID for omnimarket consumers.",
    )
    kafka_environment: str = Field(
        default="",
        description=(
            "Environment label (dev / staging / prod) for logging only. "
            "Not used for topic prefixing."
        ),
    )
    enable_kafka: bool = Field(
        default=False,
        description="Enable Kafka producer/consumer wiring. Defaults False for safety.",
    )

    redpanda_admin_host: str = Field(
        default="",
        description=(
            "Redpanda admin API host. Used by node_baseline_capture probes. "
            "REQUIRED only by probes that target Redpanda admin."
        ),
    )
    redpanda_admin_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="Redpanda admin API port (typically 9644).",
    )

    # =========================================================================
    # POSTGRESQL
    # =========================================================================
    postgres_host: str = Field(
        default="",
        description=(
            "PostgreSQL host. REQUIRED when ENABLE_POSTGRES=true and no full "
            "DSN is provided. No default to prevent silent localhost connections."
        ),
    )
    postgres_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description=(
            "PostgreSQL port. REQUIRED when ENABLE_POSTGRES=true and no full "
            "DSN is provided. Standard port is 5432. 0 = unconfigured."
        ),
    )
    postgres_database: str = Field(
        default="",
        description=(
            "PostgreSQL database name for omnimarket. REQUIRED when "
            "ENABLE_POSTGRES=true and no full DSN is provided."
        ),
    )
    postgres_user: str = Field(
        default="",
        description=(
            "PostgreSQL username. REQUIRED when ENABLE_POSTGRES=true and no "
            "full DSN is provided."
        ),
    )
    postgres_password: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description=(
            "PostgreSQL password. REQUIRED when ENABLE_POSTGRES=true and no "
            "full DSN is provided."
        ),
    )
    omnibase_infra_db_url: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description=(
            "Full PostgreSQL connection URL for the omnibase_infra database. "
            "When set, takes precedence over individual POSTGRES_* fields. "
            "Format: postgresql://user:password@host:port/dbname"
        ),
    )
    omnidash_analytics_db_url: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description=(
            "Full PostgreSQL connection URL for the omnidash_analytics "
            "projection database. When set, takes precedence over POSTGRES_* fields."
        ),
    )
    enable_postgres: bool = Field(
        default=False,
        description=(
            "Enable PostgreSQL connections. When True, either a *_DB_URL must "
            "be set or all individual POSTGRES_* fields must be configured."
        ),
    )

    # =========================================================================
    # QDRANT (vector DB)
    # =========================================================================
    qdrant_host: str = Field(
        default="",
        description="Qdrant host. REQUIRED when ENABLE_QDRANT=true.",
    )
    qdrant_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="Qdrant port. REQUIRED when ENABLE_QDRANT=true.",
    )
    enable_qdrant: bool = Field(
        default=False,
        description="Enable Qdrant connections. Defaults False for safety.",
    )

    # =========================================================================
    # VALKEY / REDIS-COMPATIBLE CACHE
    # =========================================================================
    valkey_host: str = Field(
        default="",
        description="Valkey/Redis host. REQUIRED when ENABLE_VALKEY=true.",
    )
    valkey_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="Valkey/Redis port. REQUIRED when ENABLE_VALKEY=true.",
    )
    enable_valkey: bool = Field(
        default=False,
        description="Enable Valkey/Redis connections. Defaults False for safety.",
    )

    # =========================================================================
    # LLM ENDPOINTS
    # -------------------------------------------------------------------------
    # All LLM URL / model-id pairs default to empty. Production code asks for
    # them only when an LLM-backed feature is invoked; nothing prevents the
    # rest of the system from starting without LLMs configured.
    # =========================================================================
    llm_coder_url: str = Field(
        default="",
        description=(
            "Base URL for the primary coder LLM endpoint. Empty by default; "
            "set in env when invoking coder-backed features."
        ),
    )
    llm_coder_model_id: str = Field(
        default="",
        description=(
            "Model identifier for the coder endpoint (passed verbatim to the "
            "OpenAI-compatible API)."
        ),
    )
    llm_coder_fast_url: str = Field(
        default="",
        description="Base URL for the fast/reasoning LLM endpoint.",
    )
    llm_coder_fast_model_id: str = Field(
        default="",
        description="Model identifier for the fast/reasoning endpoint.",
    )
    llm_reasoner_url: str = Field(
        default="",
        description="Base URL for the dedicated reasoner endpoint.",
    )
    llm_reasoner_model_id: str = Field(
        default="",
        description="Model identifier for the reasoner endpoint.",
    )
    llm_embedding_url: str = Field(
        default="",
        description="Base URL for the embedding endpoint.",
    )
    llm_embedding_model_id: str = Field(
        default="",
        description="Model identifier for the embedding endpoint.",
    )

    # GLM cloud fallback / cloud APIs (kept distinct from LAN endpoints).
    llm_glm_url: str = Field(
        default="",
        description="GLM cloud API base URL (e.g. https://api.z.ai).",
    )
    llm_glm_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="GLM cloud API key.",
    )
    llm_glm_model_name: str = Field(
        default="",
        description="GLM cloud model identifier.",
    )

    # Other cloud providers (some nodes use them directly).
    openai_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="OpenAI API key. Required only when an OpenAI-backed node is invoked.",
    )
    google_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="Google API key (Gemini, etc.).",
    )
    gemini_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="Gemini API key (alias for google_api_key in some nodes).",
    )
    anthropic_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="Anthropic API key. Required only when an Anthropic-backed node is invoked.",
    )

    # =========================================================================
    # GITHUB / LINEAR
    # =========================================================================
    github_token: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description=(
            "GitHub Personal Access Token. Required only by skills that call "
            "the GitHub REST/GraphQL API directly."
        ),
    )
    gh_pat: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="Alternative GitHub PAT name used by some scripts.",
    )
    linear_api_key: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description=(
            "Linear API key. Required only by skills/nodes that read or write "
            "Linear tickets."
        ),
    )

    # =========================================================================
    # MEMORY SERVICE / EMBEDDING
    # =========================================================================
    embedding_model_url: str = Field(
        default="",
        description=(
            "Embedding model URL used by code-embedding nodes. REQUIRED when "
            "ENABLE_MEMORY_SERVICE=true."
        ),
    )
    enable_memory_service: bool = Field(
        default=False,
        description="Enable embedding/memory service wiring. Defaults False.",
    )

    # =========================================================================
    # ONEX RUNTIME / EMIT DAEMON / INFRA TARGETS
    # =========================================================================
    onex_emit_socket_path: str = Field(
        default="",
        description=(
            "Path to the omniclaude emit daemon Unix socket. Read by hooks "
            "that publish events. Empty disables daemon emission."
        ),
    )
    onex_emit_spool_dir: str = Field(
        default="",
        description="Spool directory for buffered events.",
    )
    onex_emit_daemon_socket: str = Field(
        default="",
        description="Alternative emit-daemon socket path used by some scripts.",
    )
    onex_state_root: str = Field(
        default="",
        description=(
            "Override for ONEX state root. Falls back to ONEX_STATE_DIR raw "
            "env when unset (bootstrap-only)."
        ),
    )
    onex_dashboard_api: str = Field(
        default="",
        description=(
            "Base URL for the omnidash API (used by skills that query "
            "projections). Empty disables dashboard probes."
        ),
    )
    onex_infra_ssh_target: str = Field(
        default="",
        description=(
            "SSH target for infra hosts (used by deploy/redeploy nodes). No "
            "default — must be explicitly set."
        ),
    )
    onex_target_runtime_address: str = Field(
        default="",
        description="Pattern-B runtime address (used by codex adapter).",
    )

    # =========================================================================
    # API KEYS / WEBHOOKS — informational misc
    # =========================================================================
    slack_bot_token: SecretStr = Field(  # noqa: secrets — pydantic field
        default=SecretStr(""),
        description="Slack bot token (used by notification nodes).",
    )

    # ------------------------------------------------------------------------
    # Computed accessors
    # ------------------------------------------------------------------------
    @staticmethod
    def _has_text(value: str) -> bool:
        """Return true when a string contains non-whitespace text."""
        return bool(value.strip())

    def get_effective_kafka_bootstrap_servers(self) -> str:
        """Return ``kafka_bootstrap_servers`` if set, else ``kafka_broker``."""
        if self._has_text(self.kafka_bootstrap_servers):
            return self.kafka_bootstrap_servers.strip()
        return self.kafka_broker.strip()

    def get_effective_postgres_dsn(self) -> str:
        """Return the first non-empty DSN from the configured DB URLs.

        omnimarket uses two databases (`omnibase_infra` and
        `omnidash_analytics`) — callers that need a specific DB should read
        the corresponding ``*_db_url`` field directly. This convenience returns
        whichever DSN is set first.
        """
        for dsn in (self.omnibase_infra_db_url, self.omnidash_analytics_db_url):
            value = dsn.get_secret_value()
            if self._has_text(value):
                return value.strip()
        return ""

    # ------------------------------------------------------------------------
    # Required-service validation
    # ------------------------------------------------------------------------
    def validate_required_services(self) -> list[str]:
        """Return a list of named errors for missing required configuration.

        FAIL-FAST. When a service is enabled, ALL required configuration must
        be explicitly provided; there are no fallback defaults.

        Empty list means valid. Each entry names the field, the enabling flag,
        and the remediation.
        """
        errors: list[str] = []

        if self.enable_kafka and not self.get_effective_kafka_bootstrap_servers():
            errors.append(
                "KAFKA_BOOTSTRAP_SERVERS (or KAFKA_BROKER) is required when "
                "ENABLE_KAFKA=true. Set one in the environment or set "
                "ENABLE_KAFKA=false."
            )

        if self.enable_postgres:
            has_dsn = bool(self.get_effective_postgres_dsn())
            if not has_dsn:
                if not self._has_text(self.postgres_host):
                    errors.append(
                        "POSTGRES_HOST is required when ENABLE_POSTGRES=true and no "
                        "*_DB_URL is set. Set POSTGRES_HOST or a *_DB_URL, or set "
                        "ENABLE_POSTGRES=false."
                    )
                if self.postgres_port == 0:
                    errors.append(
                        "POSTGRES_PORT is required when ENABLE_POSTGRES=true and no "
                        "*_DB_URL is set. Set POSTGRES_PORT (5432 standard) or a "
                        "*_DB_URL, or set ENABLE_POSTGRES=false."
                    )
                if not self._has_text(self.postgres_database):
                    errors.append(
                        "POSTGRES_DATABASE is required when ENABLE_POSTGRES=true and "
                        "no *_DB_URL is set. Set POSTGRES_DATABASE or a *_DB_URL, "
                        "or set ENABLE_POSTGRES=false."
                    )
                if not self._has_text(self.postgres_user):
                    errors.append(
                        "POSTGRES_USER is required when ENABLE_POSTGRES=true and "
                        "no *_DB_URL is set. Set POSTGRES_USER or a *_DB_URL, "
                        "or set ENABLE_POSTGRES=false."
                    )
                if not self._has_text(self.postgres_password.get_secret_value()):
                    errors.append(
                        "POSTGRES_PASSWORD is required when ENABLE_POSTGRES=true "
                        "and no *_DB_URL is set. Set POSTGRES_PASSWORD or a "
                        "*_DB_URL, or set ENABLE_POSTGRES=false."
                    )

        if self.enable_qdrant:
            if not self._has_text(self.qdrant_host):
                errors.append(
                    "QDRANT_HOST is required when ENABLE_QDRANT=true. Set "
                    "QDRANT_HOST or set ENABLE_QDRANT=false."
                )
            if self.qdrant_port == 0:
                errors.append(
                    "QDRANT_PORT is required when ENABLE_QDRANT=true. Set "
                    "QDRANT_PORT or set ENABLE_QDRANT=false."
                )

        if self.enable_valkey:
            if not self._has_text(self.valkey_host):
                errors.append(
                    "VALKEY_HOST is required when ENABLE_VALKEY=true. Set "
                    "VALKEY_HOST or set ENABLE_VALKEY=false."
                )
            if self.valkey_port == 0:
                errors.append(
                    "VALKEY_PORT is required when ENABLE_VALKEY=true. Set "
                    "VALKEY_PORT or set ENABLE_VALKEY=false."
                )

        if self.enable_memory_service and not self._has_text(self.embedding_model_url):
            errors.append(
                "EMBEDDING_MODEL_URL is required when "
                "ENABLE_MEMORY_SERVICE=true. Set EMBEDDING_MODEL_URL or "
                "set ENABLE_MEMORY_SERVICE=false."
            )

        return errors


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance.

    Tests should construct ``Settings(...)`` directly with explicit kwargs to
    bypass the cache and avoid env contamination.
    """
    return Settings()
