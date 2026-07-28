# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed staging-composition contract, live snapshot, and readiness verdict.

OMN-15253 slice 1. The July 25-27 managed-staging cutover discovered eleven
independent runtime prerequisites *serially* — each only after the previous
layer was repaired — because no single machine-readable artifact described the
complete staging runtime. These models are that artifact:

``ModelStagingCompositionContract``
    The declared composition of one environment (cluster identity, runtime and
    migration image digests, broker budget/auth, required secret key NAMES,
    host sysctl minimums, Service selectors, migrations + schema objects,
    publisher IAM grants, workload replicas, rollback resources) plus the
    read-only probe list (``snapshot_sources``) that produces its observation.

``ModelStagingLiveSnapshot``
    The observed counterpart. **Every block is optional** — an absent block is
    not a pass, it forces ``INDETERMINATE`` on the checks that need it and
    ``BLOCKED`` overall (see ``engine_staging_readiness``).

``ModelStagingReadinessVerdict``
    The non-mutating go/no-go, with per-check findings that carry
    ``expected`` / ``observed`` / ``contract_field_path`` / ``probe_id`` so a
    BLOCKED verdict is directly actionable.

Fail-closed invariants baked into these models:

1. ``extra="forbid"`` everywhere — an unknown key in a contract document or a
   snapshot is a hard validation error, never a silently dropped field.
2. No check may be marked optional. There is no ``optional`` field to set;
   un-evaluable is expressed as ``INDETERMINATE``, never as a downgrade.
3. Secret proof is **key presence by name**. No model in this module has a
   field that can carry a secret value, and every key-name list is validated
   against ``^[A-Z][A-Z0-9_]*$`` with an error message that deliberately does
   NOT echo the offending entry — so a collector that hands values instead of
   names fails closed without leaking them into the exception text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AWS_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,32}$")


class EnumStagingReadiness(StrEnum):
    """Terminal readiness status. Only READY permits deployment."""

    READY = "READY"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class EnumStagingFindingSeverity(StrEnum):
    """Finding severity.

    Both members block. There is deliberately no ``WARNING`` — a downgrade path
    is how an un-evaluated check renders GREEN, which is the exact vacuous-green
    trap this contract exists to close.
    """

    BLOCKING = "BLOCKING"
    INDETERMINATE = "INDETERMINATE"


class EnumStagingReadinessCheck(StrEnum):
    """One member per F-01 serial discovery, plus replicas and rollback."""

    CLUSTER_IDENTITY_MATCHES = "CLUSTER_IDENTITY_MATCHES"
    IMAGE_SUPPORTS_BROKER_AUTH_MODE = "IMAGE_SUPPORTS_BROKER_AUTH_MODE"
    TOPIC_BUDGET_WITHIN_BROKER_CAPACITY = "TOPIC_BUDGET_WITHIN_BROKER_CAPACITY"
    CONSUMER_GROUP_PREFIX_EXCLUSIVE = "CONSUMER_GROUP_PREFIX_EXCLUSIVE"
    HANDLER_OWNER_PACKAGES_ACTIVE = "HANDLER_OWNER_PACKAGES_ACTIVE"
    REQUIRED_SECRET_KEYS_PRESENT = "REQUIRED_SECRET_KEYS_PRESENT"
    HOST_SYSCTL_MINIMUMS = "HOST_SYSCTL_MINIMUMS"
    SECRET_SYNC_TARGET_COVERAGE = "SECRET_SYNC_TARGET_COVERAGE"
    SERVICE_SELECTOR_EXACT = "SERVICE_SELECTOR_EXACT"
    MIGRATIONS_APPLIED = "MIGRATIONS_APPLIED"
    SCHEMA_OBJECTS_PRESENT = "SCHEMA_OBJECTS_PRESENT"
    SINGLE_SOURCE_REV_BUNDLE = "SINGLE_SOURCE_REV_BUNDLE"
    PUBLISHER_IAM_GRANTS_PRESENT = "PUBLISHER_IAM_GRANTS_PRESENT"
    WORKLOAD_REPLICAS_MATCH = "WORKLOAD_REPLICAS_MATCH"
    ROLLBACK_RESOURCES_AVAILABLE = "ROLLBACK_RESOURCES_AVAILABLE"


class EnumSecretValidationMethod(StrEnum):
    """How a required secret is proven present.

    ``PRESENCE_BY_KEY_NAME`` is the only member on purpose: value comparison and
    length-as-proof are forbidden (a length-only check already passed once while
    the wrong value was synced).
    """

    PRESENCE_BY_KEY_NAME = "presence_by_key_name"


class EnumTopicProvisioningMode(StrEnum):
    PER_CONTRACT = "per_contract"
    FULL_UNIVERSE = "full_universe"


class ModelStagingBase(BaseModel):
    """Frozen, extra-forbidding base for every model in this module."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def validate_key_names(values: list[str], field_name: str) -> list[str]:
    """Validate a list of bare env/secret KEY NAMES (never values).

    The error message names the field and the index but never the offending
    entry: if a collector mistakenly hands a secret VALUE, the rejection must
    not become the leak. Ordering is preserved so findings are deterministic.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        name = raw.strip()
        if not _KEY_NAME_RE.match(name):
            raise ValueError(
                f"{field_name}[{index}] is not a bare key NAME (expected "
                "^[A-Z][A-Z0-9_]*$). Secret values must never enter the "
                "snapshot; the rejected entry is deliberately not echoed."
            )
        if name in seen:
            raise ValueError(f"{field_name} entries must be unique: {name}")
        seen.add(name)
        normalized.append(name)
    return normalized


def _validate_non_blank_unique(values: list[str], field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = item.strip()
        if not text:
            raise ValueError(f"{field_name} entries must not be blank")
        if text in seen:
            raise ValueError(f"{field_name} entries must be unique: {text}")
        seen.add(text)
        normalized.append(text)
    return normalized


def canonical_sha256(payload: object) -> str:
    """Deterministic sha256 over a JSON-canonicalized payload. Pure."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def document_sha256(raw_document: Mapping[str, object]) -> str:
    """Self-hash of a composition DOCUMENT, over its raw parsed mapping.

    Defined on the raw mapping — not on the validated model — so the identical
    four-line algorithm is reproducible in any repo that holds an instance
    document with nothing but ``yaml`` + ``json`` + ``hashlib``. omninode_infra's
    CI gate recomputes it that way; if it were defined over the pydantic dump,
    the repo that owns the document could not check its own file without taking
    a package dependency on the repo that owns the schema.
    """
    body = {key: value for key, value in raw_document.items() if key != "sha256"}
    return canonical_sha256(body)


# ---------------------------------------------------------------------------
# Contract blocks
# ---------------------------------------------------------------------------


class ModelStagingClusterIdentity(ModelStagingBase):
    """Which cluster this composition describes — and which it explicitly is not.

    ``excluded_clusters`` is load-bearing: two k3s clusters existed and both once
    consumed the same MSK consumer groups, so "the right values on the wrong
    cluster" is a real observed failure mode, not a hypothetical.
    """

    aws_account_id: str = Field(..., description="12-digit AWS account id.")
    region: str = Field(..., min_length=1)
    instance_id: str = Field(..., description="EC2 instance id of the k3s host.")
    name_tag: str = Field(..., min_length=1)
    namespaces: list[str] = Field(..., min_length=1)
    is_canonical_beta_target: bool = Field(...)
    excluded_clusters: list[str] = Field(default_factory=list)

    @field_validator("aws_account_id")
    @classmethod
    def _check_account(cls, value: str) -> str:
        if not _AWS_ACCOUNT_RE.match(value.strip()):
            raise ValueError("aws_account_id must be 12 digits")
        return value.strip()

    @field_validator("instance_id")
    @classmethod
    def _check_instance(cls, value: str) -> str:
        text = value.strip()
        if not _INSTANCE_ID_RE.match(text):
            raise ValueError(f"instance_id is not an EC2 instance id: {text}")
        return text

    @field_validator("namespaces")
    @classmethod
    def _check_namespaces(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "namespaces")

    @field_validator("excluded_clusters")
    @classmethod
    def _check_excluded(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _INSTANCE_ID_RE.match(item.strip()):
                raise ValueError(
                    f"excluded_clusters entry is not an instance id: {item}"
                )
        return _validate_non_blank_unique(value, "excluded_clusters")


class ModelStagingImageRef(ModelStagingBase):
    """A pinned container image: repository + immutable digest + source rev."""

    repository: str = Field(..., min_length=1)
    digest: str = Field(..., description="sha256:<64 hex> — never a mutable tag.")
    source_rev: str = Field(
        ..., min_length=7, description="Git rev the image was built from."
    )

    @field_validator("digest")
    @classmethod
    def _check_digest(cls, value: str) -> str:
        text = value.strip()
        if not _DIGEST_RE.match(text):
            raise ValueError(f"digest must match sha256:<64 hex>, got: {text}")
        return text


class ModelStagingHandlerBinding(ModelStagingBase):
    """A handler the environment must be able to dispatch, and who owns it."""

    name: str = Field(..., min_length=1)
    owner_package: str = Field(..., min_length=1)


class ModelStagingRuntimeSpec(ModelStagingBase):
    """The runtime image and the package/config surface it must expose."""

    image: ModelStagingImageRef
    supported_auth_modes: list[str] = Field(..., min_length=1)
    active_runtime_packages: list[str] = Field(..., min_length=1)
    required_config_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Config key NAMES the runtime ConfigMap must declare. Single home "
            "for the set formerly duplicated in scripts/required-configmap-vars.txt."
        ),
    )

    @field_validator("supported_auth_modes", "active_runtime_packages")
    @classmethod
    def _check_unique(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "list")

    @field_validator("required_config_keys")
    @classmethod
    def _check_config_keys(cls, value: list[str]) -> list[str]:
        return validate_key_names(value, "required_config_keys")


class ModelStagingTopicBudget(ModelStagingBase):
    """Bounded topic provisioning — the guard against full-universe warm-up."""

    max_topics: int = Field(..., ge=1)
    max_partitions: int = Field(..., ge=1)
    partitions_per_topic: int = Field(..., ge=1)
    provisioning_mode: EnumTopicProvisioningMode
    universe_provision_enabled: bool


class ModelStagingBrokerCapacity(ModelStagingBase):
    broker_count: int = Field(..., ge=1)
    instance_class: str = Field(..., min_length=1)


class ModelStagingBrokerSpec(ModelStagingBase):
    """Managed-broker composition: auth, capacity, budget, group ownership."""

    auth_mode: str = Field(..., min_length=1)
    security_protocol: str = Field(..., min_length=1)
    capacity: ModelStagingBrokerCapacity
    topic_budget: ModelStagingTopicBudget
    consumer_group_patterns: list[str] = Field(..., min_length=1)
    group_prefix_exclusive_owner: str = Field(
        ...,
        description="Instance id of the ONLY cluster permitted to own these group prefixes.",
    )

    @field_validator("group_prefix_exclusive_owner")
    @classmethod
    def _check_owner(cls, value: str) -> str:
        text = value.strip()
        if not _INSTANCE_ID_RE.match(text):
            raise ValueError(
                f"group_prefix_exclusive_owner must be an instance id: {text}"
            )
        return text

    @field_validator("consumer_group_patterns")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "consumer_group_patterns")


class ModelStagingSyncTarget(ModelStagingBase):
    """Where the authoritative store must land a secret for workloads to see it."""

    kind: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)

    @property
    def target_key(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


class ModelStagingSecretRequirement(ModelStagingBase):
    """A required secret, proven by KEY NAME only.

    ``consuming_workloads`` is what makes SECRET_SYNC_TARGET_COVERAGE possible:
    "present in the authoritative store" is not the same claim as "present in
    every workload that reads it" — the analytics DSN was the second one short.
    """

    name: str
    authoritative_store: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    sync_target: ModelStagingSyncTarget
    consuming_workloads: list[str] = Field(..., min_length=1)
    required: bool = True
    validation_method: EnumSecretValidationMethod = (
        EnumSecretValidationMethod.PRESENCE_BY_KEY_NAME
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_key_names([value], "secrets[].name")[0]

    @field_validator("consuming_workloads")
    @classmethod
    def _check_workloads(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "consuming_workloads")


class ModelStagingSysctlRequirement(ModelStagingBase):
    """A host kernel minimum, plus whether it must survive a reboot."""

    min: int = Field(..., ge=0)
    durable_in_bootstrap: bool = Field(
        ...,
        description="True when the value must be encoded in host bootstrap, not just live.",
    )


class ModelStagingHostSpec(ModelStagingBase):
    sysctls: dict[str, ModelStagingSysctlRequirement] = Field(..., min_length=1)


class ModelStagingServiceSpec(ModelStagingBase):
    """A Service whose selector is compared EXACTLY.

    A superset selector is a failure, not a nicety: the runtime Service selected
    effects and worker pods as HTTP targets because its selector was broader than
    the component it was meant to front.
    """

    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    selector: dict[str, str] = Field(..., min_length=1)
    expected_endpoint_components: list[str] = Field(..., min_length=1)
    port: int = Field(..., ge=1, le=65535)

    @field_validator("expected_endpoint_components")
    @classmethod
    def _check_components(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "expected_endpoint_components")


class ModelStagingMigrationSpec(ModelStagingBase):
    required_revisions: list[str] = Field(..., min_length=1)
    image: ModelStagingImageRef

    @field_validator("required_revisions")
    @classmethod
    def _check_revisions(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "required_revisions")


class ModelStagingSchemaObject(ModelStagingBase):
    """A table the migrations must have actually produced in the live database."""

    table: str = Field(..., min_length=1)
    primary_key_columns: list[str] = Field(..., min_length=1)
    indexes: list[str] = Field(default_factory=list)
    columns: list[str] = Field(..., min_length=1)

    @field_validator("primary_key_columns", "indexes", "columns")
    @classmethod
    def _check_lists(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "schema object list")


class ModelStagingWorkload(ModelStagingBase):
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    component: str = Field(..., min_length=1)
    replicas: int = Field(..., ge=0)


class ModelStagingRollbackResource(ModelStagingBase):
    """A resource that must still exist for the documented rollback to work."""

    kind: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    expected_state: str = Field(..., min_length=1)
    capacity: str | None = Field(default=None)


class ModelStagingIamGrant(ModelStagingBase):
    principal: str = Field(..., min_length=1)
    resource: str = Field(..., min_length=1)
    actions: list[str] = Field(..., min_length=1)

    @field_validator("actions")
    @classmethod
    def _check_actions(cls, value: list[str]) -> list[str]:
        return _validate_non_blank_unique(value, "actions")


class ModelStagingPublisherSpec(ModelStagingBase):
    iam_grants: list[ModelStagingIamGrant] = Field(..., min_length=1)


class ModelStagingSnapshotSource(ModelStagingBase):
    """A declared, read-only probe — capture is contract DATA, not a script.

    ``parses_into`` is the seam between this contract and
    ``ModelStagingLiveSnapshot``: slice 2's collect EFFECT executes exactly this
    list and writes each result at exactly this dotted path, and a human running
    the preflight by hand runs the same command.
    """

    probe_id: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    read_only: bool = Field(...)
    parses_into: str = Field(
        ..., description="Dotted field path on ModelStagingLiveSnapshot."
    )
    required: bool = True

    @field_validator("read_only")
    @classmethod
    def _check_read_only(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "snapshot_sources must be read_only: preflight capture never mutates"
            )
        return value

    @field_validator("parses_into")
    @classmethod
    def _check_path(cls, value: str) -> str:
        text = value.strip()
        if not text or text.startswith(".") or text.endswith("."):
            raise ValueError(f"parses_into must be a dotted field path, got: {text!r}")
        return text


class ModelStagingCompositionContract(ModelStagingBase):
    """The complete declared composition of one staging environment."""

    schema_version: str = Field(..., description="Semver of the contract SCHEMA.")
    contract_id: str = Field(..., min_length=1)
    environment: str = Field(..., min_length=1)
    source_rev: str = Field(..., min_length=7)
    frozen_at: datetime
    sha256: str = Field(
        ..., description="Self-hash over the canonicalized body (sha256 excluded)."
    )

    cluster: ModelStagingClusterIdentity
    runtime: ModelStagingRuntimeSpec
    handlers: list[ModelStagingHandlerBinding] = Field(..., min_length=1)
    broker: ModelStagingBrokerSpec
    secrets: list[ModelStagingSecretRequirement] = Field(..., min_length=1)
    host: ModelStagingHostSpec
    services: list[ModelStagingServiceSpec] = Field(..., min_length=1)
    migrations: ModelStagingMigrationSpec
    schema_objects: list[ModelStagingSchemaObject] = Field(..., min_length=1)
    workloads: list[ModelStagingWorkload] = Field(..., min_length=1)
    rollback_resources: list[ModelStagingRollbackResource] = Field(..., min_length=1)
    publisher: ModelStagingPublisherSpec
    snapshot_sources: list[ModelStagingSnapshotSource] = Field(..., min_length=1)

    @field_validator("sha256")
    @classmethod
    def _check_sha_shape(cls, value: str) -> str:
        text = value.strip().lower()
        if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return text

    def sha256_matches_document(self, raw_document: Mapping[str, object]) -> bool:
        """True when this model's ``sha256`` is the self-hash of its source document."""
        return self.sha256 == document_sha256(raw_document)


# ---------------------------------------------------------------------------
# Live snapshot (every block optional — absence is never a pass)
# ---------------------------------------------------------------------------


class ModelObservedCluster(ModelStagingBase):
    aws_account_id: str | None = None
    region: str | None = None
    instance_id: str | None = None
    name_tag: str | None = None
    namespaces: list[str] | None = None


class ModelObservedRuntime(ModelStagingBase):
    image_digest: str | None = None
    image_source_rev: str | None = None
    supported_auth_modes: list[str] | None = None
    active_runtime_packages: list[str] | None = None
    config_key_names: list[str] | None = None

    @field_validator("config_key_names")
    @classmethod
    def _check_config_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_key_names(value, "config_key_names")


class ModelObservedBroker(ModelStagingBase):
    auth_mode: str | None = None
    security_protocol: str | None = None
    broker_count: int | None = None
    instance_class: str | None = None
    topic_count: int | None = None
    partition_count: int | None = None
    universe_provision_enabled: bool | None = None
    provisioning_mode: str | None = None
    consumer_group_ids: list[str] | None = None
    consumer_group_owner_instance_ids: list[str] | None = Field(
        default=None,
        description="Distinct cluster instance ids observed owning the group prefixes.",
    )


class ModelObservedSecrets(ModelStagingBase):
    """Observed secret KEY NAMES only. There is no field that can hold a value."""

    synced_key_names_by_target: dict[str, list[str]] | None = Field(
        default=None, description="'<kind>/<namespace>/<name>' -> key NAMES present."
    )
    workload_env_key_names: dict[str, list[str]] | None = Field(
        default=None, description="workload name -> env key NAMES injected."
    )

    @field_validator("synced_key_names_by_target", "workload_env_key_names")
    @classmethod
    def _check_key_names(
        cls, value: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        if value is None:
            return None
        return {
            key: validate_key_names(names, f"secret key names for {key}")
            for key, names in value.items()
        }


class ModelObservedHost(ModelStagingBase):
    sysctls: dict[str, int] | None = None
    sysctls_durable_in_bootstrap: dict[str, bool] | None = None


class ModelObservedService(ModelStagingBase):
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    selector: dict[str, str] | None = None
    endpoint_components: list[str] | None = None
    port: int | None = None


class ModelObservedMigrations(ModelStagingBase):
    applied_revisions: list[str] | None = None
    image_digest: str | None = None
    image_source_rev: str | None = None


class ModelObservedTable(ModelStagingBase):
    primary_key_columns: list[str] | None = None
    indexes: list[str] | None = None
    columns: list[str] | None = None


class ModelObservedSchema(ModelStagingBase):
    tables: dict[str, ModelObservedTable] | None = None


class ModelObservedWorkload(ModelStagingBase):
    name: str = Field(..., min_length=1)
    namespace: str | None = None
    component: str | None = None
    replicas: int | None = None


class ModelObservedRollbackResource(ModelStagingBase):
    kind: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    namespace: str = Field(..., min_length=1)
    observed_state: str | None = None
    capacity: str | None = None


class ModelObservedPublisher(ModelStagingBase):
    iam_grants: list[ModelStagingIamGrant] | None = None


class ModelStagingLiveSnapshot(ModelStagingBase):
    """Read-only observation of the live environment. Caller-supplied.

    Handed to the COMPUTE node already parsed — the node performs no I/O, so a
    snapshot constructed by hand (test, fixture, simulation) is sample data by
    construction and never a live verdict. Slice 2's collect EFFECT is the only
    surface that may produce a live one.
    """

    captured_at: datetime
    captured_by_probe_ids: list[str] = Field(default_factory=list)
    cluster: ModelObservedCluster | None = None
    runtime: ModelObservedRuntime | None = None
    broker: ModelObservedBroker | None = None
    secrets: ModelObservedSecrets | None = None
    host: ModelObservedHost | None = None
    services: list[ModelObservedService] | None = None
    migrations: ModelObservedMigrations | None = None
    schema_objects: ModelObservedSchema | None = None
    workloads: list[ModelObservedWorkload] | None = None
    rollback_resources: list[ModelObservedRollbackResource] | None = None
    publisher: ModelObservedPublisher | None = None

    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Request / verdict
# ---------------------------------------------------------------------------


class ModelStagingReadinessRequest(ModelStagingBase):
    """def-B request: both the contract and the snapshot arrive already parsed.

    ``evaluated_at`` is caller-supplied so the handler stays deterministic — a
    pure COMPUTE node must not read the clock any more than it reads a file.
    """

    contract: ModelStagingCompositionContract
    snapshot: ModelStagingLiveSnapshot
    evaluated_at: datetime
    correlation_id: UUID | None = None


class ModelStagingReadinessFinding(ModelStagingBase):
    check: EnumStagingReadinessCheck
    severity: EnumStagingFindingSeverity
    expected: str
    observed: str
    contract_field_path: str
    probe_id: str | None = None
    remediation_hint: str = ""


class ModelStagingReadinessProvenance(ModelStagingBase):
    contract_id: str
    schema_version: str
    contract_sha256: str
    snapshot_sha256: str
    snapshot_captured_at: datetime
    source_rev: str
    evaluated_at: datetime


class ModelStagingReadinessVerdict(ModelStagingBase):
    """Non-mutating go/no-go. ``deployment_permitted`` is True only on READY."""

    status: EnumStagingReadiness
    findings: list[ModelStagingReadinessFinding] = Field(default_factory=list)
    checks_evaluated: list[EnumStagingReadinessCheck] = Field(default_factory=list)
    blocking_findings_count: int = 0
    indeterminate_findings_count: int = 0
    deployment_permitted: bool = False
    provenance: ModelStagingReadinessProvenance
    correlation_id: UUID | None = None


__all__ = [
    "EnumSecretValidationMethod",
    "EnumStagingFindingSeverity",
    "EnumStagingReadiness",
    "EnumStagingReadinessCheck",
    "EnumTopicProvisioningMode",
    "ModelObservedBroker",
    "ModelObservedCluster",
    "ModelObservedHost",
    "ModelObservedMigrations",
    "ModelObservedPublisher",
    "ModelObservedRollbackResource",
    "ModelObservedRuntime",
    "ModelObservedSchema",
    "ModelObservedSecrets",
    "ModelObservedService",
    "ModelObservedTable",
    "ModelObservedWorkload",
    "ModelStagingBase",
    "ModelStagingBrokerCapacity",
    "ModelStagingBrokerSpec",
    "ModelStagingClusterIdentity",
    "ModelStagingCompositionContract",
    "ModelStagingHandlerBinding",
    "ModelStagingHostSpec",
    "ModelStagingIamGrant",
    "ModelStagingImageRef",
    "ModelStagingLiveSnapshot",
    "ModelStagingMigrationSpec",
    "ModelStagingPublisherSpec",
    "ModelStagingReadinessFinding",
    "ModelStagingReadinessProvenance",
    "ModelStagingReadinessRequest",
    "ModelStagingReadinessVerdict",
    "ModelStagingRollbackResource",
    "ModelStagingRuntimeSpec",
    "ModelStagingSchemaObject",
    "ModelStagingSecretRequirement",
    "ModelStagingServiceSpec",
    "ModelStagingSnapshotSource",
    "ModelStagingSyncTarget",
    "ModelStagingSysctlRequirement",
    "ModelStagingTopicBudget",
    "ModelStagingWorkload",
    "canonical_sha256",
    "document_sha256",
    "validate_key_names",
]
