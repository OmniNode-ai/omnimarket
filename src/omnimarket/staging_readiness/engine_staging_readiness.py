# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure staging-readiness evaluation engine (OMN-15253 slice 1).

Takes a declared ``ModelStagingCompositionContract`` plus a caller-supplied
``ModelStagingLiveSnapshot`` and returns a ``ModelStagingReadinessVerdict``.

The engine performs **zero I/O**: no file reads, no subprocess, no network, no
clock. ``evaluated_at`` arrives on the request. That is what makes the verdict
replayable — the same (contract, snapshot, evaluated_at) triple always produces
the same verdict, byte for byte.

Fail-closed contract, restated because it is the whole point:

* an absent snapshot field yields ``INDETERMINATE`` for its check and forces
  ``BLOCKED`` overall — absent is never a pass;
* no check can be skipped or downgraded; the only outputs are pass, BLOCKING,
  and INDETERMINATE;
* secret proof is key-presence-by-name; the engine never sees, compares, or
  emits a secret value.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence
from typing import get_args, get_origin

from pydantic import BaseModel

from omnimarket.staging_readiness.model_staging_composition import (
    EnumStagingFindingSeverity,
    EnumStagingReadiness,
    EnumStagingReadinessCheck,
    ModelStagingCompositionContract,
    ModelStagingLiveSnapshot,
    ModelStagingReadinessFinding,
    ModelStagingReadinessProvenance,
    ModelStagingReadinessRequest,
    ModelStagingReadinessVerdict,
)

_ABSENT = "<absent>"

CheckEvaluator = Callable[
    [ModelStagingCompositionContract, ModelStagingLiveSnapshot, str | None],
    list[ModelStagingReadinessFinding],
]


# ---------------------------------------------------------------------------
# Seam helpers: snapshot_sources[].parses_into <-> ModelStagingLiveSnapshot
# ---------------------------------------------------------------------------


def snapshot_field_path_exists(path: str) -> bool:
    """True when ``path`` resolves to a real field on ``ModelStagingLiveSnapshot``.

    This is the machine-checkable half of the OMN-15253 seam: every declared
    probe's ``parses_into`` must land on a field the snapshot model actually has,
    or slice 2's collect EFFECT would write results into a hole.
    """
    model: type[BaseModel] | None = ModelStagingLiveSnapshot
    for segment in path.split("."):
        if model is None:
            return False
        field = model.model_fields.get(segment)
        if field is None:
            return False
        model = _model_of(field.annotation)
    return True


def _model_of(annotation: object) -> type[BaseModel] | None:
    """Unwrap ``X | None``, ``list[X]``, ``dict[str, X]`` down to a BaseModel."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is None:
        return None
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        found = _model_of(arg)
        if found is not None:
            return found
    return None


def probe_id_for(contract: ModelStagingCompositionContract, path: str) -> str | None:
    """Longest-prefix match from a snapshot field path to its declared probe."""
    best: tuple[int, str] | None = None
    for source in contract.snapshot_sources:
        target = source.parses_into
        if path == target or path.startswith(f"{target}."):
            score = len(target)
            if best is None or score > best[0]:
                best = (score, source.probe_id)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Finding constructors
# ---------------------------------------------------------------------------


def _blocking(
    check: EnumStagingReadinessCheck,
    *,
    expected: str,
    observed: str,
    path: str,
    probe_id: str | None,
    hint: str,
) -> ModelStagingReadinessFinding:
    return ModelStagingReadinessFinding(
        check=check,
        severity=EnumStagingFindingSeverity.BLOCKING,
        expected=expected,
        observed=observed,
        contract_field_path=path,
        probe_id=probe_id,
        remediation_hint=hint,
    )


def _absent_finding(
    check: EnumStagingReadinessCheck,
    *,
    expected: str,
    path: str,
    probe_id: str | None,
    hint: str,
) -> ModelStagingReadinessFinding:
    """The fail-closed constructor: an un-observed field can never render green."""
    return ModelStagingReadinessFinding(
        check=check,
        severity=EnumStagingFindingSeverity.INDETERMINATE,
        expected=expected,
        observed=_ABSENT,
        contract_field_path=path,
        probe_id=probe_id,
        remediation_hint=hint or "capture the declared probe; absent is not a pass",
    )


def _source_rev_matches(observed: str, declared: str) -> bool:
    """Compare git revs allowing the abbreviated form registries actually carry.

    ECR tags the runtime image ``candidate-<short-rev>-<timestamp>``, so the
    observable rev is a 7+ character prefix of the declared 40-character rev.
    Equality is still required for the characters that exist — this tolerates
    abbreviation, never divergence.
    """
    left = observed.strip()
    right = declared.strip()
    if not left or not right:
        return False
    shortest = min(len(left), len(right))
    if shortest < 7:
        return False
    return left[:shortest] == right[:shortest]


def _fmt(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(sorted(str(item) for item in value)) or "<empty>"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items())) or "<empty>"
    return str(value)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_cluster_identity(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.CLUSTER_IDENTITY_MATCHES
    expected = contract.cluster
    observed = snapshot.cluster
    if observed is None:
        return [
            _absent_finding(
                check,
                expected=f"instance {expected.instance_id} in {expected.aws_account_id}",
                path="cluster",
                probe_id=probe,
                hint="run the cluster-identity probe before evaluating readiness",
            )
        ]
    findings: list[ModelStagingReadinessFinding] = []
    scalar_fields = (
        ("aws_account_id", expected.aws_account_id, observed.aws_account_id),
        ("region", expected.region, observed.region),
        ("instance_id", expected.instance_id, observed.instance_id),
        ("name_tag", expected.name_tag, observed.name_tag),
    )
    for field_name, want, got in scalar_fields:
        if got is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=str(want),
                    path=f"cluster.{field_name}",
                    probe_id=probe,
                    hint=f"probe did not report cluster.{field_name}",
                )
            )
        elif got != want:
            findings.append(
                _blocking(
                    check,
                    expected=str(want),
                    observed=str(got),
                    path=f"cluster.{field_name}",
                    probe_id=probe,
                    hint="the composition is being evaluated against the wrong cluster",
                )
            )
    if (
        observed.instance_id is not None
        and observed.instance_id in expected.excluded_clusters
    ):
        findings.append(
            _blocking(
                check,
                expected=f"not one of {_fmt(expected.excluded_clusters)}",
                observed=observed.instance_id,
                path="cluster.excluded_clusters",
                probe_id=probe,
                hint="this cluster is explicitly excluded from the canonical beta target",
            )
        )
    if observed.namespaces is None:
        findings.append(
            _absent_finding(
                check,
                expected=_fmt(expected.namespaces),
                path="cluster.namespaces",
                probe_id=probe,
                hint="probe did not report namespaces",
            )
        )
    else:
        missing = [ns for ns in expected.namespaces if ns not in observed.namespaces]
        if missing:
            findings.append(
                _blocking(
                    check,
                    expected=_fmt(expected.namespaces),
                    observed=_fmt(observed.namespaces),
                    path="cluster.namespaces",
                    probe_id=probe,
                    hint=f"missing namespace(s): {_fmt(missing)}",
                )
            )
    return findings


def _check_image_supports_auth_mode(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.IMAGE_SUPPORTS_BROKER_AUTH_MODE
    runtime = snapshot.runtime
    broker = snapshot.broker
    want_digest = contract.runtime.image.digest
    want_mode = contract.broker.auth_mode
    findings: list[ModelStagingReadinessFinding] = []
    if runtime is None or runtime.image_digest is None:
        findings.append(
            _absent_finding(
                check,
                expected=want_digest,
                path="runtime.image.digest",
                probe_id=probe,
                hint="probe the deployed runtime image digest",
            )
        )
    elif runtime.image_digest != want_digest:
        findings.append(
            _blocking(
                check,
                expected=want_digest,
                observed=runtime.image_digest,
                path="runtime.image.digest",
                probe_id=probe,
                hint=(
                    "the deployed runtime image is not the reviewed build; an older "
                    "digest may not support the selected broker auth mode at all"
                ),
            )
        )
    if runtime is None or runtime.supported_auth_modes is None:
        findings.append(
            _absent_finding(
                check,
                expected=want_mode,
                path="runtime.supported_auth_modes",
                probe_id=probe,
                hint="the image's declared auth-mode support was not observed",
            )
        )
    elif want_mode not in runtime.supported_auth_modes:
        findings.append(
            _blocking(
                check,
                expected=want_mode,
                observed=_fmt(runtime.supported_auth_modes),
                path="runtime.supported_auth_modes",
                probe_id=probe,
                hint=(
                    "the deployed image cannot speak the broker's auth mode — "
                    "rebuild/redeploy before touching broker configuration"
                ),
            )
        )
    if broker is None or broker.auth_mode is None:
        findings.append(
            _absent_finding(
                check,
                expected=want_mode,
                path="broker.auth_mode",
                probe_id=probe_id_for(contract, "broker"),
                hint="probe the live broker auth mode",
            )
        )
    elif broker.auth_mode != want_mode:
        findings.append(
            _blocking(
                check,
                expected=want_mode,
                observed=broker.auth_mode,
                path="broker.auth_mode",
                probe_id=probe_id_for(contract, "broker"),
                hint="live broker auth mode diverges from the declared composition",
            )
        )
    return findings


def _check_topic_budget(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.TOPIC_BUDGET_WITHIN_BROKER_CAPACITY
    budget = contract.broker.topic_budget
    capacity = contract.broker.capacity
    observed = snapshot.broker
    if observed is None:
        return [
            _absent_finding(
                check,
                expected=f"<= {budget.max_topics} topics on {capacity.broker_count} brokers",
                path="broker.topic_budget",
                probe_id=probe,
                hint="probe the broker before permitting startup provisioning",
            )
        ]
    findings: list[ModelStagingReadinessFinding] = []
    if observed.universe_provision_enabled is None:
        findings.append(
            _absent_finding(
                check,
                expected=str(budget.universe_provision_enabled),
                path="broker.topic_budget.universe_provision_enabled",
                probe_id=probe,
                hint="probe the runtime's universe-provisioning flag",
            )
        )
    elif observed.universe_provision_enabled != budget.universe_provision_enabled:
        findings.append(
            _blocking(
                check,
                expected=str(budget.universe_provision_enabled),
                observed=str(observed.universe_provision_enabled),
                path="broker.topic_budget.universe_provision_enabled",
                probe_id=probe,
                hint=(
                    "full-universe provisioning on a two-broker cluster attempts "
                    "more than a thousand topics at startup"
                ),
            )
        )
    if observed.provisioning_mode is None:
        findings.append(
            _absent_finding(
                check,
                expected=str(budget.provisioning_mode),
                path="broker.topic_budget.provisioning_mode",
                probe_id=probe,
                hint="probe the runtime's topic provisioning mode",
            )
        )
    elif observed.provisioning_mode != str(budget.provisioning_mode):
        findings.append(
            _blocking(
                check,
                expected=str(budget.provisioning_mode),
                observed=observed.provisioning_mode,
                path="broker.topic_budget.provisioning_mode",
                probe_id=probe,
                hint="only per-contract provisioning is safe on the managed dev broker",
            )
        )
    numeric = (
        (
            "topic_count",
            "broker.topic_budget.max_topics",
            observed.topic_count,
            budget.max_topics,
        ),
        (
            "partition_count",
            "broker.topic_budget.max_partitions",
            observed.partition_count,
            budget.max_partitions,
        ),
    )
    for label, path, got, limit in numeric:
        if got is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{label} <= {limit}",
                    path=path,
                    probe_id=probe,
                    hint=f"probe did not report {label}",
                )
            )
        elif got > limit:
            findings.append(
                _blocking(
                    check,
                    expected=f"{label} <= {limit}",
                    observed=f"{label} = {got}",
                    path=path,
                    probe_id=probe,
                    hint="declared budget exceeds what the broker fleet can carry",
                )
            )
    capacity_fields: tuple[tuple[str, str, object | None, object], ...] = (
        (
            "broker_count",
            "broker.capacity.broker_count",
            observed.broker_count,
            capacity.broker_count,
        ),
        (
            "instance_class",
            "broker.capacity.instance_class",
            observed.instance_class,
            capacity.instance_class,
        ),
    )
    for cap_label, cap_path, cap_got, cap_want in capacity_fields:
        if cap_got is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=str(cap_want),
                    path=cap_path,
                    probe_id=probe,
                    hint=f"probe did not report {cap_label}",
                )
            )
        elif cap_got != cap_want:
            findings.append(
                _blocking(
                    check,
                    expected=str(cap_want),
                    observed=str(cap_got),
                    path=cap_path,
                    probe_id=probe,
                    hint="live broker capacity diverges from the declared composition",
                )
            )
    return findings


def _check_consumer_group_exclusive(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.CONSUMER_GROUP_PREFIX_EXCLUSIVE
    owner = contract.broker.group_prefix_exclusive_owner
    patterns = contract.broker.consumer_group_patterns
    observed = snapshot.broker
    findings: list[ModelStagingReadinessFinding] = []
    if observed is None or observed.consumer_group_owner_instance_ids is None:
        findings.append(
            _absent_finding(
                check,
                expected=f"sole owner {owner}",
                path="broker.group_prefix_exclusive_owner",
                probe_id=probe,
                hint="list the group prefix's owners before cutover",
            )
        )
    else:
        foreign = [
            item for item in observed.consumer_group_owner_instance_ids if item != owner
        ]
        if foreign:
            findings.append(
                _blocking(
                    check,
                    expected=f"sole owner {owner}",
                    observed=_fmt(observed.consumer_group_owner_instance_ids),
                    path="broker.group_prefix_exclusive_owner",
                    probe_id=probe,
                    hint=(
                        "a second cluster is consuming the same group prefixes — "
                        f"foreign owner(s): {_fmt(foreign)}"
                    ),
                )
            )
    if observed is not None and observed.consumer_group_ids is not None:
        unmatched = [
            group
            for group in observed.consumer_group_ids
            if not any(fnmatch.fnmatchcase(group, pattern) for pattern in patterns)
        ]
        if unmatched:
            findings.append(
                _blocking(
                    check,
                    expected=_fmt(patterns),
                    observed=_fmt(unmatched),
                    path="broker.consumer_group_patterns",
                    probe_id=probe,
                    hint="consumer group(s) outside the declared prefix patterns",
                )
            )
    elif observed is not None:
        findings.append(
            _absent_finding(
                check,
                expected=_fmt(patterns),
                path="broker.consumer_group_patterns",
                probe_id=probe,
                hint="probe did not report consumer group ids",
            )
        )
    return findings


def _check_handler_owner_packages(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.HANDLER_OWNER_PACKAGES_ACTIVE
    required = sorted({binding.owner_package for binding in contract.handlers})
    runtime = snapshot.runtime
    if runtime is None or runtime.active_runtime_packages is None:
        return [
            _absent_finding(
                check,
                expected=_fmt(required),
                path="runtime.active_runtime_packages",
                probe_id=probe,
                hint="probe ONEX_ACTIVE_RUNTIME_PACKAGES on the live runtime",
            )
        ]
    missing = [pkg for pkg in required if pkg not in runtime.active_runtime_packages]
    if not missing:
        return []
    starved = sorted(
        binding.name
        for binding in contract.handlers
        if binding.owner_package in missing
    )
    return [
        _blocking(
            check,
            expected=_fmt(required),
            observed=_fmt(runtime.active_runtime_packages),
            path="runtime.active_runtime_packages",
            probe_id=probe,
            hint=(
                f"package(s) {_fmt(missing)} own declared handler(s) {_fmt(starved)} "
                "but are not in the runtime allowlist — those handlers are invisible "
                "at runtime while every pod stays Ready"
            ),
        )
    ]


def _secret_target_keys(
    snapshot: ModelStagingLiveSnapshot,
) -> dict[str, list[str]] | None:
    if snapshot.secrets is None:
        return None
    return snapshot.secrets.synced_key_names_by_target


def _check_required_secret_keys(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.REQUIRED_SECRET_KEYS_PRESENT
    by_target = _secret_target_keys(snapshot)
    required = [secret for secret in contract.secrets if secret.required]
    if by_target is None:
        return [
            _absent_finding(
                check,
                expected=_fmt([secret.name for secret in required]),
                path="secrets[].name",
                probe_id=probe,
                hint="read the synced Secret's KEY NAMES (never its values)",
            )
        ]
    findings: list[ModelStagingReadinessFinding] = []
    for index, secret in enumerate(required):
        target = secret.sync_target.target_key
        present = by_target.get(target)
        if present is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{secret.name} in {target}",
                    path=f"secrets[{index}].sync_target",
                    probe_id=probe,
                    hint=f"sync target {target} was not observed at all",
                )
            )
        elif secret.name not in present:
            findings.append(
                _blocking(
                    check,
                    expected=f"key name {secret.name} present in {target}",
                    observed=f"{len(present)} key name(s) synced, {secret.name} not among them",
                    path=f"secrets[{index}].name",
                    probe_id=probe,
                    hint=(
                        f"{secret.owner} must sync {secret.name} from "
                        f"{secret.authoritative_store} into {target}"
                    ),
                )
            )
    return findings


def _check_secret_sync_coverage(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.SECRET_SYNC_TARGET_COVERAGE
    observed = (
        snapshot.secrets.workload_env_key_names
        if snapshot.secrets is not None
        else None
    )
    required = [secret for secret in contract.secrets if secret.required]
    if observed is None:
        return [
            _absent_finding(
                check,
                expected="per-workload key coverage for every required secret",
                path="secrets[].consuming_workloads",
                probe_id=probe,
                hint="read each consuming workload's injected env KEY NAMES",
            )
        ]
    findings: list[ModelStagingReadinessFinding] = []
    for index, secret in enumerate(required):
        for workload in secret.consuming_workloads:
            injected = observed.get(workload)
            if injected is None:
                findings.append(
                    _absent_finding(
                        check,
                        expected=f"{secret.name} injected into {workload}",
                        path=f"secrets[{index}].consuming_workloads",
                        probe_id=probe,
                        hint=f"workload {workload} was not observed",
                    )
                )
            elif secret.name not in injected:
                findings.append(
                    _blocking(
                        check,
                        expected=f"{secret.name} injected into {workload}",
                        observed=f"{workload} does not inject {secret.name}",
                        path=f"secrets[{index}].consuming_workloads",
                        probe_id=probe,
                        hint=(
                            "present in the authoritative store is not the same claim "
                            "as present in every workload that reads it"
                        ),
                    )
                )
    return findings


def _check_host_sysctls(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.HOST_SYSCTL_MINIMUMS
    host = snapshot.host
    findings: list[ModelStagingReadinessFinding] = []
    for name, requirement in sorted(contract.host.sysctls.items()):
        observed_value = (
            None if host is None or host.sysctls is None else host.sysctls.get(name)
        )
        if observed_value is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{name} >= {requirement.min}",
                    path=f"host.sysctls.{name}",
                    probe_id=probe,
                    hint=f"read {name} on the host before deploying",
                )
            )
        elif observed_value < requirement.min:
            findings.append(
                _blocking(
                    check,
                    expected=f"{name} >= {requirement.min}",
                    observed=f"{name} = {observed_value}",
                    path=f"host.sysctls.{name}",
                    probe_id=probe,
                    hint="below the kernel minimum the runtime/SSM workers need to start",
                )
            )
        if not requirement.durable_in_bootstrap:
            continue
        durable_map = (
            None
            if host is None or host.sysctls_durable_in_bootstrap is None
            else host.sysctls_durable_in_bootstrap
        )
        durable = None if durable_map is None else durable_map.get(name)
        if durable is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{name} durable in host bootstrap",
                    path=f"host.sysctls.{name}.durable_in_bootstrap",
                    probe_id=probe,
                    hint="a live-only raise is lost on the next reboot",
                )
            )
        elif not durable:
            findings.append(
                _blocking(
                    check,
                    expected=f"{name} durable in host bootstrap",
                    observed=f"{name} set live only",
                    path=f"host.sysctls.{name}.durable_in_bootstrap",
                    probe_id=probe,
                    hint="encode the raise in host bootstrap; live-only reverts on reboot",
                )
            )
    return findings


def _check_service_selectors(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.SERVICE_SELECTOR_EXACT
    observed_services = {
        (service.namespace, service.name): service
        for service in (snapshot.services or [])
    }
    findings: list[ModelStagingReadinessFinding] = []
    for index, expected in enumerate(contract.services):
        observed = observed_services.get((expected.namespace, expected.name))
        if observed is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"Service {expected.namespace}/{expected.name}",
                    path=f"services[{index}]",
                    probe_id=probe,
                    hint="probe the Service and its Endpoints",
                )
            )
            continue
        if observed.selector is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=_fmt(expected.selector),
                    path=f"services[{index}].selector",
                    probe_id=probe,
                    hint="probe did not report the Service selector",
                )
            )
        elif observed.selector != expected.selector:
            extra = {
                key: value
                for key, value in observed.selector.items()
                if expected.selector.get(key) != value
            }
            missing = {
                key: value
                for key, value in expected.selector.items()
                if observed.selector.get(key) != value
            }
            findings.append(
                _blocking(
                    check,
                    expected=_fmt(expected.selector),
                    observed=_fmt(observed.selector),
                    path=f"services[{index}].selector",
                    probe_id=probe,
                    hint=(
                        "selectors are compared exactly — a broader selector silently "
                        "adds effects/worker pods as HTTP targets "
                        f"(unexpected: {_fmt(extra)}; missing: {_fmt(missing)})"
                    ),
                )
            )
        if observed.port is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=str(expected.port),
                    path=f"services[{index}].port",
                    probe_id=probe,
                    hint="probe did not report the Service port",
                )
            )
        elif observed.port != expected.port:
            findings.append(
                _blocking(
                    check,
                    expected=str(expected.port),
                    observed=str(observed.port),
                    path=f"services[{index}].port",
                    probe_id=probe,
                    hint="Service port diverges from the declared composition",
                )
            )
        if observed.endpoint_components is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=_fmt(expected.expected_endpoint_components),
                    path=f"services[{index}].expected_endpoint_components",
                    probe_id=probe,
                    hint="probe the Endpoints object, not just the Service",
                )
            )
        elif sorted(observed.endpoint_components) != sorted(
            expected.expected_endpoint_components
        ):
            findings.append(
                _blocking(
                    check,
                    expected=_fmt(expected.expected_endpoint_components),
                    observed=_fmt(observed.endpoint_components),
                    path=f"services[{index}].expected_endpoint_components",
                    probe_id=probe,
                    hint="the Service is fronting pods it was never meant to front",
                )
            )
    return findings


def _check_migrations_applied(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.MIGRATIONS_APPLIED
    required = contract.migrations.required_revisions
    observed = snapshot.migrations
    if observed is None or observed.applied_revisions is None:
        return [
            _absent_finding(
                check,
                expected=_fmt(required),
                path="migrations.required_revisions",
                probe_id=probe,
                hint="read the applied revisions from the live database",
            )
        ]
    missing = [rev for rev in required if rev not in observed.applied_revisions]
    if not missing:
        return []
    return [
        _blocking(
            check,
            expected=_fmt(required),
            observed=_fmt(observed.applied_revisions),
            path="migrations.required_revisions",
            probe_id=probe,
            hint=(
                f"revision(s) {_fmt(missing)} exist in source but were never applied "
                "to this database — source presence is not application"
            ),
        )
    ]


def _check_schema_objects(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.SCHEMA_OBJECTS_PRESENT
    observed_tables = (
        snapshot.schema_objects.tables if snapshot.schema_objects is not None else None
    )
    findings: list[ModelStagingReadinessFinding] = []
    for index, expected in enumerate(contract.schema_objects):
        observed = (
            None if observed_tables is None else observed_tables.get(expected.table)
        )
        if observed is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"table {expected.table}",
                    path=f"schema_objects[{index}].table",
                    probe_id=probe,
                    hint="read information_schema for the declared table",
                )
            )
            continue
        for label, want, got in (
            ("columns", expected.columns, observed.columns),
            (
                "primary_key_columns",
                expected.primary_key_columns,
                observed.primary_key_columns,
            ),
            ("indexes", expected.indexes, observed.indexes),
        ):
            if not want:
                continue
            if got is None:
                findings.append(
                    _absent_finding(
                        check,
                        expected=_fmt(want),
                        path=f"schema_objects[{index}].{label}",
                        probe_id=probe,
                        hint=f"probe did not report {label} for {expected.table}",
                    )
                )
                continue
            missing = [item for item in want if item not in got]
            if missing:
                findings.append(
                    _blocking(
                        check,
                        expected=_fmt(want),
                        observed=_fmt(got),
                        path=f"schema_objects[{index}].{label}",
                        probe_id=probe,
                        hint=(
                            f"{expected.table} is missing {label}: {_fmt(missing)} — "
                            "the table exists but is the wrong shape"
                        ),
                    )
                )
    return findings


def _check_single_source_rev_bundle(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.SINGLE_SOURCE_REV_BUNDLE
    runtime = snapshot.runtime
    migrations = snapshot.migrations
    findings: list[ModelStagingReadinessFinding] = []
    want_digest = contract.migrations.image.digest
    if migrations is None or migrations.image_digest is None:
        findings.append(
            _absent_finding(
                check,
                expected=want_digest,
                path="migrations.image.digest",
                probe_id=probe,
                hint="probe the deployed migration image digest",
            )
        )
    elif migrations.image_digest != want_digest:
        findings.append(
            _blocking(
                check,
                expected=want_digest,
                observed=migrations.image_digest,
                path="migrations.image.digest",
                probe_id=probe,
                hint=(
                    "the deployed migration image is not the reviewed build — a stale "
                    "pin silently omits every newer revision"
                ),
            )
        )
    revs = {
        "runtime": None if runtime is None else runtime.image_source_rev,
        "migrations": None if migrations is None else migrations.image_source_rev,
    }
    for label, rev in revs.items():
        if rev is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=contract.source_rev,
                    path=f"{'runtime' if label == 'runtime' else 'migrations'}.image.source_rev",
                    probe_id=probe,
                    hint=f"probe did not report the {label} image source rev",
                )
            )
        elif not _source_rev_matches(rev, contract.source_rev):
            findings.append(
                _blocking(
                    check,
                    expected=contract.source_rev,
                    observed=rev,
                    path=f"{'runtime' if label == 'runtime' else 'migrations'}.image.source_rev",
                    probe_id=probe,
                    hint=(
                        "runtime and migration images must be built from ONE source "
                        "revision — a split bundle is how source and live disagree"
                    ),
                )
            )
    return findings


def _check_publisher_iam_grants(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.PUBLISHER_IAM_GRANTS_PRESENT
    observed = snapshot.publisher.iam_grants if snapshot.publisher is not None else None
    if observed is None:
        return [
            _absent_finding(
                check,
                expected=_fmt(
                    [grant.resource for grant in contract.publisher.iam_grants]
                ),
                path="publisher.iam_grants",
                probe_id=probe,
                hint="read the publisher role's inline policy",
            )
        ]
    by_key = {(grant.principal, grant.resource): grant for grant in observed}
    findings: list[ModelStagingReadinessFinding] = []
    for index, expected in enumerate(contract.publisher.iam_grants):
        got = by_key.get((expected.principal, expected.resource))
        if got is None:
            findings.append(
                _blocking(
                    check,
                    expected=f"{expected.principal} -> {expected.resource}",
                    observed=_fmt(sorted(f"{p} -> {r}" for p, r in by_key)),
                    path=f"publisher.iam_grants[{index}]",
                    probe_id=probe,
                    hint=(
                        "the publisher cannot push the image it is required to "
                        "produce; the build fails after CI is already green"
                    ),
                )
            )
            continue
        missing = [action for action in expected.actions if action not in got.actions]
        if missing:
            findings.append(
                _blocking(
                    check,
                    expected=_fmt(expected.actions),
                    observed=_fmt(got.actions),
                    path=f"publisher.iam_grants[{index}].actions",
                    probe_id=probe,
                    hint=f"grant exists but is missing action(s): {_fmt(missing)}",
                )
            )
    return findings


def _check_workload_replicas(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.WORKLOAD_REPLICAS_MATCH
    observed = {workload.name: workload for workload in (snapshot.workloads or [])}
    findings: list[ModelStagingReadinessFinding] = []
    for index, expected in enumerate(contract.workloads):
        got = observed.get(expected.name)
        if got is None or got.replicas is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{expected.name} = {expected.replicas}",
                    path=f"workloads[{index}].replicas",
                    probe_id=probe,
                    hint=f"probe did not report replicas for {expected.name}",
                )
            )
        elif got.replicas != expected.replicas:
            findings.append(
                _blocking(
                    check,
                    expected=f"{expected.name} = {expected.replicas}",
                    observed=f"{expected.name} = {got.replicas}",
                    path=f"workloads[{index}].replicas",
                    probe_id=probe,
                    hint="live replica count diverges from the declared composition",
                )
            )
    return findings


def _check_rollback_resources(
    contract: ModelStagingCompositionContract,
    snapshot: ModelStagingLiveSnapshot,
    probe: str | None,
) -> list[ModelStagingReadinessFinding]:
    check = EnumStagingReadinessCheck.ROLLBACK_RESOURCES_AVAILABLE
    observed = {
        (item.kind, item.namespace, item.name): item
        for item in (snapshot.rollback_resources or [])
    }
    findings: list[ModelStagingReadinessFinding] = []
    for index, expected in enumerate(contract.rollback_resources):
        got = observed.get((expected.kind, expected.namespace, expected.name))
        if got is None or got.observed_state is None:
            findings.append(
                _absent_finding(
                    check,
                    expected=f"{expected.kind} {expected.namespace}/{expected.name} = {expected.expected_state}",
                    path=f"rollback_resources[{index}]",
                    probe_id=probe,
                    hint="a rollback path that cannot be observed is not a rollback path",
                )
            )
            continue
        if got.observed_state != expected.expected_state:
            findings.append(
                _blocking(
                    check,
                    expected=expected.expected_state,
                    observed=got.observed_state,
                    path=f"rollback_resources[{index}].expected_state",
                    probe_id=probe,
                    hint="the documented rollback target is not in its expected state",
                )
            )
        if expected.capacity is not None and got.capacity != expected.capacity:
            findings.append(
                _blocking(
                    check,
                    expected=str(expected.capacity),
                    observed=str(got.capacity),
                    path=f"rollback_resources[{index}].capacity",
                    probe_id=probe,
                    hint="rollback volume capacity diverges from the declared composition",
                )
            )
    return findings


_CHECKS: tuple[tuple[EnumStagingReadinessCheck, str, CheckEvaluator], ...] = (
    (
        EnumStagingReadinessCheck.CLUSTER_IDENTITY_MATCHES,
        "cluster",
        _check_cluster_identity,
    ),
    (
        EnumStagingReadinessCheck.IMAGE_SUPPORTS_BROKER_AUTH_MODE,
        "runtime",
        _check_image_supports_auth_mode,
    ),
    (
        EnumStagingReadinessCheck.TOPIC_BUDGET_WITHIN_BROKER_CAPACITY,
        "broker",
        _check_topic_budget,
    ),
    (
        EnumStagingReadinessCheck.CONSUMER_GROUP_PREFIX_EXCLUSIVE,
        "broker",
        _check_consumer_group_exclusive,
    ),
    (
        EnumStagingReadinessCheck.HANDLER_OWNER_PACKAGES_ACTIVE,
        "runtime",
        _check_handler_owner_packages,
    ),
    (
        EnumStagingReadinessCheck.REQUIRED_SECRET_KEYS_PRESENT,
        "secrets",
        _check_required_secret_keys,
    ),
    (EnumStagingReadinessCheck.HOST_SYSCTL_MINIMUMS, "host", _check_host_sysctls),
    (
        EnumStagingReadinessCheck.SECRET_SYNC_TARGET_COVERAGE,
        "secrets",
        _check_secret_sync_coverage,
    ),
    (
        EnumStagingReadinessCheck.SERVICE_SELECTOR_EXACT,
        "services",
        _check_service_selectors,
    ),
    (
        EnumStagingReadinessCheck.MIGRATIONS_APPLIED,
        "migrations",
        _check_migrations_applied,
    ),
    (
        EnumStagingReadinessCheck.SCHEMA_OBJECTS_PRESENT,
        "schema_objects",
        _check_schema_objects,
    ),
    (
        EnumStagingReadinessCheck.SINGLE_SOURCE_REV_BUNDLE,
        "migrations",
        _check_single_source_rev_bundle,
    ),
    (
        EnumStagingReadinessCheck.PUBLISHER_IAM_GRANTS_PRESENT,
        "publisher",
        _check_publisher_iam_grants,
    ),
    (
        EnumStagingReadinessCheck.WORKLOAD_REPLICAS_MATCH,
        "workloads",
        _check_workload_replicas,
    ),
    (
        EnumStagingReadinessCheck.ROLLBACK_RESOURCES_AVAILABLE,
        "rollback_resources",
        _check_rollback_resources,
    ),
)

ALL_CHECKS: tuple[EnumStagingReadinessCheck, ...] = tuple(
    check for check, _snapshot_block, _evaluator in _CHECKS
)


def evaluate_staging_readiness(
    request: ModelStagingReadinessRequest,
) -> ModelStagingReadinessVerdict:
    """Evaluate a live snapshot against a declared composition. Pure, no I/O."""
    contract = request.contract
    snapshot = request.snapshot

    findings: list[ModelStagingReadinessFinding] = []
    checks_evaluated: list[EnumStagingReadinessCheck] = []
    indeterminate_checks: set[EnumStagingReadinessCheck] = set()

    for check, snapshot_block, evaluator in _CHECKS:
        checks_evaluated.append(check)
        produced = evaluator(contract, snapshot, probe_id_for(contract, snapshot_block))
        findings.extend(produced)
        if any(
            item.severity is EnumStagingFindingSeverity.INDETERMINATE
            for item in produced
        ):
            indeterminate_checks.add(check)

    blocking_count = sum(
        1 for item in findings if item.severity is EnumStagingFindingSeverity.BLOCKING
    )
    indeterminate_count = sum(
        1
        for item in findings
        if item.severity is EnumStagingFindingSeverity.INDETERMINATE
    )

    if indeterminate_count and len(indeterminate_checks) == len(checks_evaluated):
        # Nothing at all could be evaluated: say so, do not call it BLOCKED as if
        # a real defect had been observed.
        status = EnumStagingReadiness.INDETERMINATE
    elif blocking_count or indeterminate_count:
        status = EnumStagingReadiness.BLOCKED
    else:
        status = EnumStagingReadiness.READY

    return ModelStagingReadinessVerdict(
        status=status,
        findings=findings,
        checks_evaluated=checks_evaluated,
        blocking_findings_count=blocking_count,
        indeterminate_findings_count=indeterminate_count,
        deployment_permitted=status is EnumStagingReadiness.READY,
        provenance=ModelStagingReadinessProvenance(
            contract_id=contract.contract_id,
            schema_version=contract.schema_version,
            contract_sha256=contract.sha256,
            snapshot_sha256=snapshot.content_sha256(),
            snapshot_captured_at=snapshot.captured_at,
            source_rev=contract.source_rev,
            evaluated_at=request.evaluated_at,
        ),
        correlation_id=request.correlation_id,
    )


def findings_for_check(
    verdict: ModelStagingReadinessVerdict, check: EnumStagingReadinessCheck
) -> Sequence[ModelStagingReadinessFinding]:
    """Convenience accessor used by callers and tests."""
    return [item for item in verdict.findings if item.check is check]


__all__ = [
    "ALL_CHECKS",
    "evaluate_staging_readiness",
    "findings_for_check",
    "probe_id_for",
    "snapshot_field_path_exists",
]
