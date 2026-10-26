# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared facts, versioned rules and decisions for exemplar admission (OMN-21128)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.exemplar_admission import (
    ModelAdmissionDecision as ModelAdmissionDecision,
)
from omnimarket.models.exemplar_admission import (
    ModelAdmissionViolation as ModelAdmissionViolation,
)
from omnimarket.models.exemplar_admission import (
    ModelExemplarAdmissionResult as ModelExemplarAdmissionResult,
)
from omnimarket.models.exemplar_admission import (
    ModelGateReceipt as ModelGateReceipt,
)
from omnimarket.models.exemplar_admission import (
    NodeArchetype as NodeArchetype,
)
from omnimarket.models.exemplar_admission import (
    ViolationCode as ViolationCode,
)


class ModelBindingClassification(BaseModel):
    """One contract binding's canonical-handler-shape classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handler_module: str
    category: str
    is_canonical: bool
    detail: str | None = None


class ModelCheckRun(BaseModel):
    """One unaggregated check-run API record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    head_sha: str
    status: str
    conclusion: str | None


class ModelTestReceipt(BaseModel):
    """Check runs and collected node test paths read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_check_names: list[str]
    check_runs: list[ModelCheckRun]
    collected_test_paths: list[str]
    node_test_path: str


class ModelExemplarCandidate(BaseModel):
    """A candidate at the request's commit, without caller-asserted success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    node_archetype: NodeArchetype
    contract_path: str
    handler_module: str
    handler_class: str
    node_package: str
    bindings: list[ModelBindingClassification]
    sources: dict[str, str]
    contract_valid: bool
    unit_test_dir_present: bool
    coverage_percent: float | None = Field(default=None, ge=0, le=100)
    test_receipt: ModelTestReceipt | None = None


class ModelExemplarAdmissionRules(BaseModel):
    """Versioned admission policy supplied as declared data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules_version: str = "exemplar-admission-rules.v1"
    pure_import_list_version: str = "pure-import-deny.v1"
    gate_absent_repositories: tuple[str, ...] = (
        "omnibase_spi",
        "omnibase_compat",
        "omnidash",
        "onex_change_control",
    )
    impure_import_modules: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: {
            "network": (
                "socket",
                "ssl",
                "http",
                "urllib",
                "requests",
                "httpx",
                "aiohttp",
                "websockets",
                "grpc",
            ),
            "filesystem": ("os", "shutil", "tempfile", "glob", "fileinput"),
            "database": (
                "sqlite3",
                "psycopg",
                "psycopg2",
                "asyncpg",
                "sqlalchemy",
                "pymongo",
                "redis",
                "motor",
            ),
            "bus": ("kafka", "confluent_kafka", "aiokafka"),
            "subprocess": ("subprocess", "multiprocessing", "pty"),
            "clock": ("time",),
        }
    )
    clock_calls: tuple[str, ...] = (
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.date.today",
    )
    deny_secret_name_pattern: str = (
        r"(?i)(password|passwd|api_?key|secret_?key|access_?token)"
    )
    deny_call_names: tuple[str, ...] = ("eval", "exec")
    deny_import_modules: tuple[str, ...] = ("pickle", "cPickle", "dill", "shelve")
    untyped_annotations: tuple[str, ...] = (
        "Any",
        "object",
        "dict",
        "Dict",
        "list",
        "List",
        "None",
        "typing.Any",
        "typing.Dict",
    )


DEFAULT_ADMISSION_RULES = ModelExemplarAdmissionRules()


class ModelExemplarAdmissionRequest(BaseModel):
    """Repository facts at a commit and the policy used to decide admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    repository_private: bool = False
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    gate: str = "canonical-handler-shape"
    gate_version: str
    baseline_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    baseline_node_ids: list[str]
    candidates: list[ModelExemplarCandidate]
    rules: ModelExemplarAdmissionRules = DEFAULT_ADMISSION_RULES
