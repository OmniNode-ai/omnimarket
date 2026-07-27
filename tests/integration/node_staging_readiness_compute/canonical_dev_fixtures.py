# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the OMN-15253 staging-readiness tests.

``contract_payload()`` mirrors
``omninode_infra/k8s/onex-dev/readiness/staging-composition.canonical-dev.v1.yaml``.

``repaired_snapshot_payload()`` is the environment as it stood AFTER the
July 25-27 cutover was finished; each RED test mutates exactly one field of it
back to the broken state that was actually observed during the cutover, so the
negative controls prove *exists-but-wrong*, not merely *absent*.

Both are hand-authored typed payloads and are therefore **sample data by
construction** — never a live readiness verdict. Live capture is slice 2.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from omnimarket.staging_readiness.model_staging_composition import (
    ModelStagingCompositionContract,
    ModelStagingLiveSnapshot,
    ModelStagingReadinessRequest,
)

CLUSTER_INSTANCE_ID = "i-06169517a92b45f86"
LEGACY_INSTANCE_ID = "i-0e596e8b557e27785"  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
SOURCE_REV = "f7fb7cdeba293003bfcb2e5eb92d8ac8acc1665b"
RUNTIME_DIGEST = (
    "sha256:a270c8fb654f573ae6d866659240b4f0b3c46ede0198fd97130f789438efad21"
)
MIGRATION_DIGEST = (
    "sha256:6a433490894523fcde3bccbd1a6489754f7ffe83ab44ad03a674de63b7adc7d1"
)
# The March build the staging migration Job was pinned to before the repair.
STALE_MIGRATION_DIGEST = "sha256:" + ("3" * 64)
STALE_MIGRATION_SOURCE_REV = "9fe653da70c137e67eac9797ecb9a27f1680c062"
RUNTIME_SELECTOR = {
    "app.kubernetes.io/name": "omninode-runtime",
    "app.kubernetes.io/component": "runtime-main",
    "omninode/env": "dev",
    "omninode/managed": "true",
    "omninode/plane": "runtime",
}
SYNC_TARGET = "Secret/onex-dev/onex-runtime-credentials"
SECRET_KEY_NAMES = [
    "GEMINI_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "LLM_GLM_API_KEY",
    "OMNIDASH_ANALYTICS_DB_URL",
]
DELEGATION_COLUMNS = [
    "correlation_id",
    "tenant_id",
    "state",
    "in_flight",
    "payload",
    "version",
    "created_at",
    "updated_at",
    "pending_emissions",
    "publish_attempts",
]
EVALUATED_AT = datetime(2026, 7, 27, 17, 0, tzinfo=UTC)


def _secret(name: str, workloads: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "authoritative_store": "infisical",
        "owner": "platform",
        "sync_target": {
            "kind": "Secret",
            "name": "onex-runtime-credentials",
            "namespace": "onex-dev",
        },
        "consuming_workloads": workloads,
        "required": True,
        "validation_method": "presence_by_key_name",
    }


def contract_payload() -> dict[str, Any]:
    """The canonical-dev composition, as a mutable payload."""
    return copy.deepcopy(
        {
            "schema_version": "1.0.0",
            "contract_id": "staging-composition.canonical-dev",
            "environment": "canonical-dev-staging",
            "source_rev": SOURCE_REV,
            "frozen_at": "2026-07-27T17:00:00Z",
            "sha256": "0" * 64,
            "cluster": {
                "aws_account_id": "272493677981",  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
                "region": "us-east-1",
                "instance_id": CLUSTER_INSTANCE_ID,
                "name_tag": "omninode-k3s-dev-system",
                "namespaces": ["onex-dev", "data-plane"],
                "is_canonical_beta_target": True,
                "excluded_clusters": [LEGACY_INSTANCE_ID],
            },
            "runtime": {
                "image": {
                    "repository": "omninode-runtime",
                    "digest": RUNTIME_DIGEST,
                    "source_rev": SOURCE_REV,
                },
                "supported_auth_modes": ["AWS_MSK_IAM"],
                "active_runtime_packages": ["omnibase_infra", "omnimarket"],
                "required_config_keys": [
                    "ONEX_ACTIVE_RUNTIME_PACKAGES",
                    "ONEX_BOOT_UNIVERSE_PROVISION",
                    "KAFKA_SECURITY_PROTOCOL",
                ],
            },
            "handlers": [
                {"name": "node_delegation_orchestrator", "owner_package": "omnimarket"},
                {
                    "name": "node_contract_loader_effect",
                    "owner_package": "omnibase_infra",
                },
            ],
            "broker": {
                "auth_mode": "AWS_MSK_IAM",
                "security_protocol": "SASL_SSL",
                "capacity": {"broker_count": 2, "instance_class": "kafka.m5.large"},
                "topic_budget": {
                    "max_topics": 2000,
                    "max_partitions": 2000,
                    "partitions_per_topic": 1,
                    "provisioning_mode": "per_contract",
                    "universe_provision_enabled": False,
                },
                "consumer_group_patterns": ["dev.*"],
                "group_prefix_exclusive_owner": CLUSTER_INSTANCE_ID,
            },
            "secrets": [
                _secret(
                    "GEMINI_API_KEY", ["omninode-runtime", "omninode-runtime-effects"]
                ),
                _secret(
                    "OPEN_ROUTER_API_KEY",
                    ["omninode-runtime", "omninode-runtime-effects"],
                ),
                _secret(
                    "LLM_GLM_API_KEY", ["omninode-runtime", "omninode-runtime-effects"]
                ),
                _secret("OMNIDASH_ANALYTICS_DB_URL", ["omninode-runtime"]),
            ],
            "host": {
                "sysctls": {
                    "fs.inotify.max_user_instances": {
                        "min": 512,
                        "durable_in_bootstrap": True,
                    }
                }
            },
            "services": [
                {
                    "name": "omninode-runtime",
                    "namespace": "onex-dev",
                    "selector": dict(RUNTIME_SELECTOR),
                    "expected_endpoint_components": ["runtime-main"],
                    "port": 8085,
                }
            ],
            "migrations": {
                "required_revisions": ["090", "093"],
                "image": {
                    "repository": "omnibase-infra-migrate",
                    "digest": MIGRATION_DIGEST,
                    "source_rev": SOURCE_REV,
                },
            },
            "schema_objects": [
                {
                    "table": "delegation_workflow_state",
                    "primary_key_columns": ["correlation_id"],
                    "indexes": [
                        "ix_delegation_workflow_state_stale_sweep",
                        "ix_delegation_workflow_state_recoverable_outbox",
                    ],
                    "columns": list(DELEGATION_COLUMNS),
                }
            ],
            "publisher": {
                "iam_grants": [
                    {
                        "principal": (
                            "arn:aws:iam::272493677981:role/omninode-github-actions-role"  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
                        ),
                        "resource": (
                            "arn:aws:ecr:us-east-1:*:repository/omnibase-infra-migrate"
                        ),
                        "actions": ["ecr:PutImage", "ecr:InitiateLayerUpload"],
                    }
                ]
            },
            "workloads": [
                {
                    "name": "omninode-runtime",
                    "namespace": "onex-dev",
                    "component": "runtime-main",
                    "replicas": 1,
                },
                {
                    "name": "omninode-runtime-effects",
                    "namespace": "onex-dev",
                    "component": "runtime-effects",
                    "replicas": 1,
                },
                {
                    "name": "omninode-runtime-worker",
                    "namespace": "onex-dev",
                    "component": "runtime-worker",
                    "replicas": 0,
                },
            ],
            "rollback_resources": [
                {
                    "kind": "StatefulSet",
                    "name": "omninode-redpanda",
                    "namespace": "data-plane",
                    "expected_state": "0/0",
                },
                {
                    "kind": "PersistentVolumeClaim",
                    "name": "data-omninode-redpanda-0",
                    "namespace": "data-plane",
                    "expected_state": "Bound",
                    "capacity": "20Gi",
                },
            ],
            "snapshot_sources": [
                {
                    "probe_id": "cluster_identity",
                    "command": f"aws ec2 describe-instances --instance-ids {CLUSTER_INSTANCE_ID}",
                    "read_only": True,
                    "parses_into": "cluster",
                    "required": True,
                },
                {
                    "probe_id": "runtime_image_and_config",
                    "command": "kubectl -n onex-dev get configmap onex-runtime-config -o json",
                    "read_only": True,
                    "parses_into": "runtime",
                    "required": True,
                },
                {
                    "probe_id": "broker_state",
                    "command": "aws kafka list-clusters --region us-east-1",
                    "read_only": True,
                    "parses_into": "broker",
                    "required": True,
                },
                {
                    "probe_id": "secret_key_names",
                    "command": (
                        "kubectl -n onex-dev get secret onex-runtime-credentials "
                        "-o jsonpath='{.data}' | jq 'keys'"
                    ),
                    "read_only": True,
                    "parses_into": "secrets",
                    "required": True,
                },
                {
                    "probe_id": "host_sysctls",
                    "command": "sysctl fs.inotify.max_user_instances",
                    "read_only": True,
                    "parses_into": "host",
                    "required": True,
                },
                {
                    "probe_id": "service_endpoints",
                    "command": "kubectl -n onex-dev get svc,endpoints -o json",
                    "read_only": True,
                    "parses_into": "services",
                    "required": True,
                },
                {
                    "probe_id": "migrations_and_schema",
                    "command": 'psql -Atc "select version_num from alembic_version"',
                    "read_only": True,
                    "parses_into": "migrations",
                    "required": True,
                },
                {
                    "probe_id": "schema_objects",
                    "command": (
                        'psql -Atc "select column_name from information_schema.columns"'
                    ),
                    "read_only": True,
                    "parses_into": "schema_objects",
                    "required": True,
                },
                {
                    "probe_id": "publisher_iam",
                    "command": (
                        "aws iam get-role-policy --role-name omninode-github-actions-role"
                    ),
                    "read_only": True,
                    "parses_into": "publisher",
                    "required": True,
                },
                {
                    "probe_id": "runtime_workloads",
                    "command": "kubectl -n onex-dev get deploy -o json",
                    "read_only": True,
                    "parses_into": "workloads",
                    "required": True,
                },
                {
                    "probe_id": "rollback_resources",
                    "command": "kubectl get sts,pvc -A -o json",
                    "read_only": True,
                    "parses_into": "rollback_resources",
                    "required": True,
                },
            ],
        }
    )


def repaired_snapshot_payload() -> dict[str, Any]:
    """The environment AFTER the cutover was completed — the GREEN baseline."""
    return copy.deepcopy(
        {
            "captured_at": "2026-07-27T16:55:00Z",
            "captured_by_probe_ids": ["cluster_identity", "broker_state"],
            "cluster": {
                "aws_account_id": "272493677981",  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
                "region": "us-east-1",
                "instance_id": CLUSTER_INSTANCE_ID,
                "name_tag": "omninode-k3s-dev-system",
                "namespaces": ["onex-dev", "data-plane", "dev"],
            },
            "runtime": {
                "image_digest": RUNTIME_DIGEST,
                "image_source_rev": "f7fb7cd",
                "supported_auth_modes": ["AWS_MSK_IAM"],
                "active_runtime_packages": ["omnibase_infra", "omnimarket"],
                "config_key_names": [
                    "ONEX_ACTIVE_RUNTIME_PACKAGES",
                    "ONEX_BOOT_UNIVERSE_PROVISION",
                    "KAFKA_SECURITY_PROTOCOL",
                ],
            },
            "broker": {
                "auth_mode": "AWS_MSK_IAM",
                "security_protocol": "SASL_SSL",
                "broker_count": 2,
                "instance_class": "kafka.m5.large",
                "topic_count": 42,
                "partition_count": 42,
                "universe_provision_enabled": False,
                "provisioning_mode": "per_contract",
                "consumer_group_ids": ["dev.delegation", "dev.projection"],
                "consumer_group_owner_instance_ids": [CLUSTER_INSTANCE_ID],
            },
            "secrets": {
                "synced_key_names_by_target": {SYNC_TARGET: list(SECRET_KEY_NAMES)},
                "workload_env_key_names": {
                    "omninode-runtime": list(SECRET_KEY_NAMES),
                    "omninode-runtime-effects": [
                        "GEMINI_API_KEY",
                        "OPEN_ROUTER_API_KEY",
                        "LLM_GLM_API_KEY",
                    ],
                },
            },
            "host": {
                "sysctls": {"fs.inotify.max_user_instances": 512},
                "sysctls_durable_in_bootstrap": {"fs.inotify.max_user_instances": True},
            },
            "services": [
                {
                    "name": "omninode-runtime",
                    "namespace": "onex-dev",
                    "selector": dict(RUNTIME_SELECTOR),
                    "endpoint_components": ["runtime-main"],
                    "port": 8085,
                }
            ],
            "migrations": {
                "applied_revisions": ["088", "089", "090", "091", "092", "093"],
                "image_digest": MIGRATION_DIGEST,
                "image_source_rev": SOURCE_REV,
            },
            "schema_objects": {
                "tables": {
                    "delegation_workflow_state": {
                        "primary_key_columns": ["correlation_id"],
                        "indexes": [
                            "ix_delegation_workflow_state_stale_sweep",
                            "ix_delegation_workflow_state_recoverable_outbox",
                        ],
                        "columns": list(DELEGATION_COLUMNS),
                    }
                }
            },
            "publisher": {
                "iam_grants": [
                    {
                        "principal": (
                            "arn:aws:iam::272493677981:role/omninode-github-actions-role"  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
                        ),
                        "resource": (
                            "arn:aws:ecr:us-east-1:*:repository/omnibase-infra-migrate"
                        ),
                        "actions": [
                            "ecr:PutImage",
                            "ecr:InitiateLayerUpload",
                            "ecr:UploadLayerPart",
                        ],
                    }
                ]
            },
            "workloads": [
                {
                    "name": "omninode-runtime",
                    "namespace": "onex-dev",
                    "component": "runtime-main",
                    "replicas": 1,
                },
                {
                    "name": "omninode-runtime-effects",
                    "namespace": "onex-dev",
                    "component": "runtime-effects",
                    "replicas": 1,
                },
                {
                    "name": "omninode-runtime-worker",
                    "namespace": "onex-dev",
                    "component": "runtime-worker",
                    "replicas": 0,
                },
            ],
            "rollback_resources": [
                {
                    "kind": "StatefulSet",
                    "name": "omninode-redpanda",
                    "namespace": "data-plane",
                    "observed_state": "0/0",
                },
                {
                    "kind": "PersistentVolumeClaim",
                    "name": "data-omninode-redpanda-0",
                    "namespace": "data-plane",
                    "observed_state": "Bound",
                    "capacity": "20Gi",
                },
            ],
        }
    )


def build_contract(
    payload: dict[str, Any] | None = None,
) -> ModelStagingCompositionContract:
    return ModelStagingCompositionContract.model_validate(payload or contract_payload())


def build_snapshot(payload: dict[str, Any] | None = None) -> ModelStagingLiveSnapshot:
    return ModelStagingLiveSnapshot.model_validate(
        payload if payload is not None else repaired_snapshot_payload()
    )


def build_request(
    contract: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> ModelStagingReadinessRequest:
    return ModelStagingReadinessRequest(
        contract=build_contract(contract),
        snapshot=build_snapshot(snapshot),
        evaluated_at=EVALUATED_AT,
    )
