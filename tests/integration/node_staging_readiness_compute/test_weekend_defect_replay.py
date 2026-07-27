# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-then-GREEN replay of every July 25-27 staging discovery (OMN-15253).

Each case takes the repaired snapshot and mutates ONE field back to the state
that was actually observed during the cutover, then asserts the evaluator
produces a **specific BLOCKING finding** on the check that owns that discovery.
The negative controls are *exists-but-wrong* (a real image digest that is the
wrong one, a real selector that is too broad, a real inotify value that is too
low) rather than *absent*, because a checker that only notices absence would
have passed every one of these on the day they broke.

The GREEN half of every case is the shared
``test_repaired_snapshot_is_ready`` assertion: the same contract against the
unmutated repaired snapshot must be READY with zero findings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.nodes.node_staging_readiness_compute.handlers.handler_staging_readiness_compute import (
    HandlerStagingReadinessCompute,
)
from omnimarket.staging_readiness.engine_staging_readiness import findings_for_check
from omnimarket.staging_readiness.model_staging_composition import (
    EnumStagingFindingSeverity,
    EnumStagingReadiness,
    EnumStagingReadinessCheck,
)
from tests.integration.node_staging_readiness_compute.canonical_dev_fixtures import (
    LEGACY_INSTANCE_ID,
    STALE_MIGRATION_DIGEST,
    STALE_MIGRATION_SOURCE_REV,
    build_request,
    repaired_snapshot_payload,
)

Mutation = Callable[[dict[str, Any]], None]


def _wrong_cluster(snapshot: dict[str, Any]) -> None:
    snapshot["cluster"]["instance_id"] = LEGACY_INSTANCE_ID


def _image_cannot_speak_auth_mode(snapshot: dict[str, Any]) -> None:
    # The pre-#2491 runtime build: a real, deployed, healthy image that simply
    # did not implement the selected MSK auth configuration.
    snapshot["runtime"]["image_digest"] = "sha256:" + ("b" * 64)
    snapshot["runtime"]["supported_auth_modes"] = ["SASL_SSL/SCRAM-SHA-512"]


def _universe_provisioning_on(snapshot: dict[str, Any]) -> None:
    snapshot["broker"]["universe_provision_enabled"] = True
    snapshot["broker"]["provisioning_mode"] = "full_universe"
    snapshot["broker"]["topic_count"] = 1200


def _foreign_cluster_shares_groups(snapshot: dict[str, Any]) -> None:
    snapshot["broker"]["consumer_group_owner_instance_ids"] = [
        snapshot["cluster"]["instance_id"],
        LEGACY_INSTANCE_ID,
    ]


def _omnimarket_dropped_from_allowlist(snapshot: dict[str, Any]) -> None:
    snapshot["runtime"]["active_runtime_packages"] = ["omnibase_infra"]


def _provider_key_not_synced(snapshot: dict[str, Any]) -> None:
    target = next(iter(snapshot["secrets"]["synced_key_names_by_target"]))
    snapshot["secrets"]["synced_key_names_by_target"][target] = [
        key
        for key in snapshot["secrets"]["synced_key_names_by_target"][target]
        if key != "GEMINI_API_KEY"
    ]


def _inotify_limit_too_low(snapshot: dict[str, Any]) -> None:
    snapshot["host"]["sysctls"]["fs.inotify.max_user_instances"] = 128


def _analytics_dsn_missing_from_workload(snapshot: dict[str, Any]) -> None:
    # Present in the store and in the synced Secret, absent from the workload
    # that reads it — the exact distinction SECRET_SYNC_TARGET_COVERAGE exists for.
    snapshot["secrets"]["workload_env_key_names"]["omninode-runtime"] = [
        key
        for key in snapshot["secrets"]["workload_env_key_names"]["omninode-runtime"]
        if key != "OMNIDASH_ANALYTICS_DB_URL"
    ]


def _service_selector_too_broad(snapshot: dict[str, Any]) -> None:
    selector = snapshot["services"][0]["selector"]
    del selector["app.kubernetes.io/component"]
    snapshot["services"][0]["endpoint_components"] = [
        "runtime-main",
        "runtime-effects",
        "runtime-worker",
    ]


def _migrations_never_applied(snapshot: dict[str, Any]) -> None:
    snapshot["migrations"]["applied_revisions"] = ["088", "089"]


def _schema_missing_outbox_columns(snapshot: dict[str, Any]) -> None:
    table = snapshot["schema_objects"]["tables"]["delegation_workflow_state"]
    table["columns"] = [
        column
        for column in table["columns"]
        if column not in {"pending_emissions", "publish_attempts"}
    ]
    table["indexes"] = ["ix_delegation_workflow_state_stale_sweep"]


def _march_migration_image(snapshot: dict[str, Any]) -> None:
    snapshot["migrations"]["image_digest"] = STALE_MIGRATION_DIGEST
    snapshot["migrations"]["image_source_rev"] = STALE_MIGRATION_SOURCE_REV


def _publisher_missing_ecr_grant(snapshot: dict[str, Any]) -> None:
    snapshot["publisher"]["iam_grants"] = [
        {
            "principal": "arn:aws:iam::272493677981:role/omninode-github-actions-role",  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
            "resource": "arn:aws:ecr:us-east-1:*:repository/omninode-runtime",
            "actions": ["ecr:PutImage"],
        }
    ]


def _effects_scaled_to_zero(snapshot: dict[str, Any]) -> None:
    for workload in snapshot["workloads"]:
        if workload["name"] == "omninode-runtime-effects":
            workload["replicas"] = 0


def _rollback_pvc_unbound(snapshot: dict[str, Any]) -> None:
    # Exists-but-wrong: the PVC is still there, so a presence-only check passes,
    # but it is Lost — the retained rollback path is not actually a rollback path.
    for item in snapshot["rollback_resources"]:
        if item["kind"] == "PersistentVolumeClaim":
            item["observed_state"] = "Lost"


CASES: list[tuple[str, Mutation, EnumStagingReadinessCheck, str]] = [
    (
        "wrong-cluster",
        _wrong_cluster,
        EnumStagingReadinessCheck.CLUSTER_IDENTITY_MATCHES,
        LEGACY_INSTANCE_ID,
    ),
    (
        "image-cannot-speak-msk-iam",
        _image_cannot_speak_auth_mode,
        EnumStagingReadinessCheck.IMAGE_SUPPORTS_BROKER_AUTH_MODE,
        "SCRAM",
    ),
    (
        "universe-topic-provisioning",
        _universe_provisioning_on,
        EnumStagingReadinessCheck.TOPIC_BUDGET_WITHIN_BROKER_CAPACITY,
        "True",
    ),
    (
        "legacy-cluster-shares-consumer-groups",
        _foreign_cluster_shares_groups,
        EnumStagingReadinessCheck.CONSUMER_GROUP_PREFIX_EXCLUSIVE,
        LEGACY_INSTANCE_ID,
    ),
    (
        "omnimarket-missing-from-active-packages",
        _omnimarket_dropped_from_allowlist,
        EnumStagingReadinessCheck.HANDLER_OWNER_PACKAGES_ACTIVE,
        "omnimarket",
    ),
    (
        "provider-key-absent-from-synced-secret",
        _provider_key_not_synced,
        EnumStagingReadinessCheck.REQUIRED_SECRET_KEYS_PRESENT,
        "GEMINI_API_KEY",
    ),
    (
        "inotify-limit-128",
        _inotify_limit_too_low,
        EnumStagingReadinessCheck.HOST_SYSCTL_MINIMUMS,
        "128",
    ),
    (
        "analytics-dsn-absent-from-workload",
        _analytics_dsn_missing_from_workload,
        EnumStagingReadinessCheck.SECRET_SYNC_TARGET_COVERAGE,
        "OMNIDASH_ANALYTICS_DB_URL",
    ),
    (
        "service-selector-too-broad",
        _service_selector_too_broad,
        EnumStagingReadinessCheck.SERVICE_SELECTOR_EXACT,
        "runtime-main",
    ),
    (
        "migrations-090-093-never-applied",
        _migrations_never_applied,
        EnumStagingReadinessCheck.MIGRATIONS_APPLIED,
        "090",
    ),
    (
        "delegation-table-wrong-shape",
        _schema_missing_outbox_columns,
        EnumStagingReadinessCheck.SCHEMA_OBJECTS_PRESENT,
        "pending_emissions",
    ),
    (
        "march-migration-image-split-source-rev",
        _march_migration_image,
        EnumStagingReadinessCheck.SINGLE_SOURCE_REV_BUNDLE,
        STALE_MIGRATION_SOURCE_REV,
    ),
    (
        "publisher-missing-ecr-grant",
        _publisher_missing_ecr_grant,
        EnumStagingReadinessCheck.PUBLISHER_IAM_GRANTS_PRESENT,
        "omnibase-infra-migrate",
    ),
    (
        "effects-runtime-scaled-to-zero",
        _effects_scaled_to_zero,
        EnumStagingReadinessCheck.WORKLOAD_REPLICAS_MATCH,
        "omninode-runtime-effects",
    ),
    (
        "rollback-pvc-lost",
        _rollback_pvc_unbound,
        EnumStagingReadinessCheck.ROLLBACK_RESOURCES_AVAILABLE,
        "Lost",
    ),
]


def test_repaired_snapshot_is_ready() -> None:
    """GREEN control: the post-cutover composition passes every check."""
    verdict = HandlerStagingReadinessCompute().handle(build_request())
    assert verdict.findings == [], [
        (item.check, item.expected, item.observed) for item in verdict.findings
    ]
    assert verdict.status is EnumStagingReadiness.READY
    assert verdict.deployment_permitted is True
    assert verdict.blocking_findings_count == 0
    assert verdict.indeterminate_findings_count == 0


@pytest.mark.parametrize(
    ("mutate", "check", "needle"),
    [pytest.param(m, c, n, id=i) for i, m, c, n in CASES],
)
def test_weekend_defect_is_blocking(
    mutate: Mutation, check: EnumStagingReadinessCheck, needle: str
) -> None:
    snapshot = repaired_snapshot_payload()
    mutate(snapshot)

    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=snapshot))

    matched = findings_for_check(verdict, check)
    assert matched, (
        f"{check} produced no finding for a snapshot that reproduces the observed "
        f"defect — the checker would have passed this on the day it broke"
    )
    assert any(
        item.severity is EnumStagingFindingSeverity.BLOCKING for item in matched
    ), f"{check} downgraded a real defect below BLOCKING: {matched}"
    rendered = " ".join(
        f"{item.expected} {item.observed} {item.remediation_hint}" for item in matched
    )
    assert needle in rendered, f"{check} finding did not name {needle!r}: {rendered}"
    assert verdict.status is EnumStagingReadiness.BLOCKED
    assert verdict.deployment_permitted is False


@pytest.mark.parametrize(
    ("mutate", "check"),
    [pytest.param(m, c, id=i) for i, m, c, _ in CASES],
)
def test_repairing_the_defect_clears_that_check(
    mutate: Mutation, check: EnumStagingReadinessCheck
) -> None:
    """The GREEN half, per discovery: undoing the mutation clears the finding."""
    broken = repaired_snapshot_payload()
    mutate(broken)
    assert findings_for_check(
        HandlerStagingReadinessCompute().handle(build_request(snapshot=broken)), check
    )

    repaired = repaired_snapshot_payload()
    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=repaired))
    assert findings_for_check(verdict, check) == []
    assert verdict.status is EnumStagingReadiness.READY


def test_every_check_has_a_negative_control() -> None:
    """No check may ship without a defect that proves it fires."""
    covered = {check for _id, _mutate, check, _needle in CASES}
    missing = sorted(set(EnumStagingReadinessCheck) - covered)
    assert not missing, f"checks with no RED test: {missing}"
