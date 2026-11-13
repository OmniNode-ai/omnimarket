# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and seam tests of the host role baseline (OMN-24263).

Golden: two hosts of one role get the same expected units and clones; a host of another role differs only by the
two roles' own entries. Failing controls: a per-host entry never reaches the list; a baseline entry added to one role
reaches every host of that role and no other. Error: no role, no baseline and a conflicting clone are refused with a
typed reason. Seam: the derived profile is what the host drift check takes, and the check reports a unit the role
declares and the host lacks.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_host_profile import HandlerHostProfileCompute
from omnimarket.models.model_host_profile import (
    EnumDriftKind,
    ModelHostDriftCheckRequest,
    ModelHostMeasurement,
    ModelHostProfile,
    ModelHostProfileDeriveRequest,
    ModelProfileClone,
)
from omnimarket.models.model_host_role import EnumHostRole
from omnimarket.nodes.node_host_drift_check_effect.handlers.handler_host_drift_check import (
    HandlerHostDriftCheck,
)
from omnimarket.nodes.node_host_role_baseline_compute.handlers.handler_host_role_baseline import (
    HandlerHostRoleBaseline,
)
from omnimarket.nodes.node_host_role_baseline_compute.models import (
    EnumBaselineRefusal,
    ModelHostRoleBaselineResult,
    ModelHostRoleBaselines,
    ModelRoleBaseline,
)

from .support import baselines, example_document, request

pytestmark = pytest.mark.unit


def _derive(
    host: str, role: EnumHostRole | None, **kw: Any
) -> ModelHostRoleBaselineResult:
    return HandlerHostRoleBaseline().handle(request(host, role, **kw))


def _profile(host: str, role: EnumHostRole) -> ModelHostProfile:
    profile = _derive(host, role).profile
    assert profile is not None
    return profile


def test_two_hosts_of_one_role_get_the_same_expected_set() -> None:
    one = _profile("h-one", EnumHostRole.LAB_WORKER)
    two = _profile("h-two", EnumHostRole.LAB_WORKER)
    assert one.systemd_units == two.systemd_units
    assert one.clones == two.clones
    assert one.systemd_units == (
        "example-drift.timer",
        "example-serve.service",
        "example-tmp-reaper.timer",
    )
    assert (one.host, two.host) == ("h-one", "h-two")


def test_a_host_of_another_role_differs_only_by_the_role_entries() -> None:
    worker = _profile("h-one", EnumHostRole.LAB_WORKER)
    runtime = _profile("h-two", EnumHostRole.RUNTIME_HOST)
    base = set(baselines().base.systemd_units)
    assert base <= set(worker.systemd_units)
    assert base <= set(runtime.systemd_units)
    assert set(worker.systemd_units) - base == {"example-tmp-reaper.timer"}
    assert set(runtime.systemd_units) - base == {"example-deploy-agent.service"}
    assert [c.repo for c in runtime.clones] == ["Example-Org/core", "Example-Org/infra"]
    assert [c.repo for c in worker.clones] == ["Example-Org/core"]


def test_the_registry_root_and_ssh_target_are_the_requests_and_the_rest_is_the_overlays() -> (
    None
):
    profile = _derive(
        "h", EnumHostRole.OPERATOR_MAC, registry_root="/Users/x/reg"
    ).profile
    assert profile is not None
    assert profile.registry_root == "/Users/x/reg"
    assert profile.ssh_target is None
    assert profile.python == "3.13"  # the role's own interpreter
    assert profile.unit_prefix == "example-"
    assert _profile("h", EnumHostRole.GPU_MODEL).python == "3.12"  # the defaults'


def test_a_baseline_entry_added_to_one_role_reaches_that_role_only() -> None:
    document = example_document()
    document["roles"]["gpu-model"]["systemd_units"].append("example-thermal.service")
    changed = ModelHostRoleBaselines.model_validate(document)
    gpu = _derive("h", EnumHostRole.GPU_MODEL, changed=changed).profile
    worker = _derive("h", EnumHostRole.LAB_WORKER, changed=changed).profile
    assert gpu is not None
    assert worker is not None
    assert "example-thermal.service" in gpu.systemd_units
    assert "example-thermal.service" not in worker.systemd_units


def test_a_host_with_no_declared_role_is_refused_not_guessed() -> None:
    answer = _derive("h-new", None)
    assert answer.profile is None
    assert answer.refusal == EnumBaselineRefusal.NO_ROLE
    assert "h-new" in answer.detail


def test_a_role_with_no_baseline_is_refused() -> None:
    document = example_document()
    del document["roles"]["gpu-model"]
    answer = _derive(
        "h",
        EnumHostRole.GPU_MODEL,
        changed=ModelHostRoleBaselines.model_validate(document),
    )
    assert answer.profile is None
    assert answer.refusal == EnumBaselineRefusal.NO_BASELINE
    assert "gpu-model" in answer.detail


def test_a_role_clone_that_disagrees_with_the_base_at_one_path_is_refused() -> None:
    document = example_document()
    document["roles"]["lab-worker"]["clones"] = [
        {"repo": "Example-Org/core", "ref": "other", "path": "$OMNI_HOME/core"}
    ]
    answer = _derive(
        "h",
        EnumHostRole.LAB_WORKER,
        changed=ModelHostRoleBaselines.model_validate(document),
    )
    assert answer.profile is None
    assert answer.refusal == EnumBaselineRefusal.CONFLICTING_CLONE


def test_an_overlay_naming_an_unknown_role_or_a_host_is_refused() -> None:
    document = example_document()
    document["roles"]["gpu_box"] = document["roles"]["gpu-model"]
    with pytest.raises(ValidationError, match="gpu_box"):
        ModelHostRoleBaselines.model_validate(document)
    document = example_document()
    document["roles"]["gpu-model"]["host"] = "h201"
    with pytest.raises(ValidationError, match="host"):
        ModelHostRoleBaselines.model_validate(document)


def test_an_overlay_missing_a_key_is_refused_and_names_it() -> None:
    document = example_document()
    del document["defaults"]["unit_prefix"]
    with pytest.raises(ValidationError, match="unit_prefix"):
        ModelHostRoleBaselines.model_validate(document)


def test_the_baseline_model_carries_no_host_field() -> None:
    assert "host" not in ModelRoleBaseline.model_fields
    assert "host" not in ModelHostRoleBaselines.model_fields


class _Measured:
    """A host that carries every declared clone and venv and only the units it is given."""

    def __init__(self, units: tuple[str, ...]) -> None:
        self._units = units

    def measure(self, profile: ModelHostProfile) -> ModelHostMeasurement:
        derived = HandlerHostProfileCompute().handle(
            ModelHostProfileDeriveRequest(profile=profile)
        )
        return ModelHostMeasurement(
            host=profile.host,
            git_dirs_in_registry=(),
            present_clone_paths=tuple(c.path for c in derived.clones),
            present_venvs=tuple(v.venv_path for v in derived.venvs),
            units=self._units,
        )


def test_the_derived_profile_is_what_the_host_drift_check_takes() -> None:
    profile = _profile("h", EnumHostRole.LAB_WORKER)
    declared = HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )
    assert declared.systemd_units == profile.systemd_units
    in_sync = HandlerHostDriftCheck(_Measured(profile.systemd_units)).handle(
        ModelHostDriftCheckRequest(profile=profile)
    )
    assert in_sync.ok
    # a unit the role declares and the host lacks, and one the host carries and no role declares
    lacking = HandlerHostDriftCheck(
        _Measured(
            ("example-drift.timer", "example-serve.service", "example-stray.service")
        )
    ).handle(ModelHostDriftCheckRequest(profile=profile))
    assert {(f.kind, f.subject) for f in lacking.findings} == {
        (EnumDriftKind.MISSING_UNIT, "example-tmp-reaper.timer"),
        (EnumDriftKind.UNDECLARED_UNIT, "example-stray.service"),
    }


def test_a_clone_row_of_the_baseline_is_a_profile_clone() -> None:
    clone = baselines().base.clones[0]
    assert isinstance(clone, ModelProfileClone)
    assert clone.path == "$OMNI_HOME/core"
