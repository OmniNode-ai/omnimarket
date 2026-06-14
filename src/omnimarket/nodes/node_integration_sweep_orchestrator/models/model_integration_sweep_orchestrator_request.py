from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_golden_chain_descriptor import (
    ModelGoldenChainDescriptor,
)


class ModelIntegrationSweepOrchestratorRequest(BaseModel):
    """Typed start command for the integration sweep orchestrator.

    ``correlation_id`` defaults when absent so the typed command validates
    against the runtime-injected envelope correlation_id on the canonical
    ``onex node`` / ``onex run`` dispatch path (OMN-13145; mirrors
    ``ModelDodVerifyStartCommand``). Without this field the local runtime
    adapter raised ``extra_forbidden`` when it built the model from the
    envelope payload dict that carries ``correlation_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        default_factory=uuid4, description="Sweep run correlation ID."
    )
    scope: str = Field(default="", description="Scope of the integration sweep.")
    tickets: list[str] = Field(
        default_factory=list,
        description="Explicit ticket IDs to include in the artifact.",
    )
    artifact_root: str = Field(
        default="",
        description=(
            "Optional root for drift/integration output. Defaults to ONEX_CC_REPO_PATH "
            "or the current working directory."
        ),
    )
    contracts_dir: str = Field(
        default="",
        description="Optional directory containing contracts/<ticket>.yaml. Defaults to artifact_root/contracts.",
    )
    receipts_dir: str = Field(
        default="",
        description="Optional directory for drift/dod_receipts. Defaults to artifact_root/drift/dod_receipts.",
    )
    runtime_host: str = Field(
        default="192.168.86.201",  # onex-allow-internal-ip OMN-9334 reason="default runtime host for SHA probe; overridden by caller or env; not a shipping connection string"
        description="Runtime SSH host for runtime_sha_match probes.",
    )
    runtime_repo_path: str = Field(
        default="/data/omninode/omni_home/omnimarket",
        description="Repo path on the runtime host used by the phase-1 SSH git SHA probe.",
    )
    artifact_date: str = Field(
        default="",
        description="ISO date used in the artifact filename. Defaults to today.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, compute the artifact path but do not write it.",
    )
    run_surface_probes: bool = Field(
        default=True,
        description=(
            "When true, execute the configured surface probes: RUNTIME_HEALTH, "
            "CONTAINER_HEALTH, GITHUB_CI, plus KAFKA / DB / PROJECTION / "
            "GOLDEN_CHAIN when their config fields are populated."
        ),
    )
    stability_test_runtime_url: str = Field(
        default="http://192.168.86.201:18085",  # onex-allow-internal-ip OMN-7538 reason="stability-test lane health endpoint; overridden by caller; not a shipping connection string"
        description="URL for the stability-test runtime health endpoint (RUNTIME_HEALTH probe).",
    )
    container_health_host: str = Field(
        default="192.168.86.201",  # onex-allow-internal-ip OMN-7538 reason="stability-test Docker host for container health probe; overridden by caller; not a shipping connection string"
        description="SSH host for the CONTAINER_HEALTH probe (docker ps).",
    )
    github_ci_repo: str = Field(
        default="omnimarket",
        description="GitHub repo name (without org prefix) used by the GITHUB_CI probe.",
    )

    # --- Infrastructure-surface probe config (KAFKA / DB / PROJECTION / GOLDEN_CHAIN) ---
    # Each probe runs only when its config list is non-empty, so an unconfigured
    # caller still gets the health/CI baseline and never a spurious failure.
    infra_runtime_host: str = Field(
        default="192.168.86.201",  # onex-allow-internal-ip OMN-13145 reason="runtime lane SSH host for rpk/psql probes; overridden by caller; not a shipping connection string"
        description="SSH host for the KAFKA / DB / GOLDEN_CHAIN probes (rpk + psql via docker exec).",
    )
    redpanda_container: str = Field(
        default="omnibase-infra-redpanda",
        description="Redpanda container name on the runtime lane for the KAFKA probe.",
    )
    postgres_container: str = Field(
        default="omnibase-infra-postgres",
        description="Postgres container name on the runtime lane for the DB probe.",
    )
    postgres_user: str = Field(
        default="postgres",
        description="Postgres role used by the DB / GOLDEN_CHAIN probes.",
    )
    kafka_topics: list[str] = Field(
        default_factory=list,
        description="Topics the KAFKA probe must find on the lane's Redpanda.",
    )
    kafka_consumer_groups: list[str] = Field(
        default_factory=list,
        description="Consumer groups the KAFKA probe must find registered on the lane.",
    )
    db_database: str = Field(
        default="omnidash_analytics",
        description="Postgres database the DB probe inspects for tail tables.",
    )
    db_tables: list[str] = Field(
        default_factory=list,
        description="Tail tables the DB probe checks for existence + row presence.",
    )
    projection_api_url: str = Field(
        default="http://192.168.86.201:3002",  # onex-allow-internal-ip OMN-13145 reason="dev-lane projection-api host; overridden by caller; not a shipping connection string"
        description="Base URL for the projection API served by the runtime lane.",
    )
    projection_topics: list[str] = Field(
        default_factory=list,
        description="Projection topics the PROJECTION probe reads via /projection/<topic>.",
    )
    golden_chains: list[ModelGoldenChainDescriptor] = Field(
        default_factory=list,
        description="Golden-chain descriptors the GOLDEN_CHAIN probe asserts head->consumer->tail.",
    )
