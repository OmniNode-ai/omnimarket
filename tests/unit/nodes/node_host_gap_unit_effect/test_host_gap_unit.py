# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""host_gap_unit: four idempotent installer units, dry-run by default, profile as the only input."""

from pathlib import Path

import pytest

from omnimarket.handlers.handler_host_profile import (
    liveness_rows_text,
    parse_host_profile,
    prepush_target_files,
)
from omnimarket.handlers.handler_host_shell import (
    action_script,
)
from omnimarket.models.model_host_profile import (
    EnumGapActionKind,
    EnumGapUnit,
    ModelGapAction,
    ModelHostGapUnitRequest,
    ModelHostGapUnitResult,
    ModelHostMeasurement,
    ModelHostProfile,
)
from omnimarket.nodes.node_host_gap_unit_effect.handlers.handler_host_gap_unit import (
    HandlerHostGapUnit,
)

EXAMPLE_PROFILES = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_host_profile_compute/host_profiles.example.yaml"
)


def load_host(host: str) -> ModelHostProfile:
    return parse_host_profile(EXAMPLE_PROFILES.read_text(), host)


class FakeHost:
    """A host that starts with a measurement and changes it as actions apply."""

    def __init__(self, seen: ModelHostMeasurement) -> None:
        self.seen = seen
        self.applied: list[ModelGapAction] = []

    def measure(self, profile: ModelHostProfile) -> ModelHostMeasurement:
        return self.seen

    def apply(self, profile: ModelHostProfile, action: ModelGapAction) -> None:
        self.applied.append(action)
        s = self.seen
        if action.kind is EnumGapActionKind.CLONE:
            self.seen = s.model_copy(
                update={"present_clone_paths": (*s.present_clone_paths, action.path)}
            )
        elif action.kind is EnumGapActionKind.VENV:
            self.seen = s.model_copy(
                update={"present_venvs": (*s.present_venvs, action.path)}
            )
        elif action.kind is EnumGapActionKind.MKDIR:
            self.seen = s.model_copy(update={"workroot_present": True})
        elif action.unit is EnumGapUnit.G3_LIVENESS:
            assert profile.liveness is not None
            ids = tuple(u.native_id for u in profile.liveness.units)
            self.seen = s.model_copy(update={"liveness_native_ids": ids})
        else:
            rows = {**s.prepush_rows, action.path: tuple(action.content.splitlines())}
            self.seen = s.model_copy(update={"prepush_rows": rows})


def bare(profile: ModelHostProfile) -> ModelHostMeasurement:
    return ModelHostMeasurement(
        host=profile.host,
        git_dirs_in_registry=(),
        present_clone_paths=(),
        present_venvs=(),
        units=(),
    )


def run(
    profile: ModelHostProfile, host: FakeHost, unit: EnumGapUnit, apply: bool = False
) -> ModelHostGapUnitResult:
    return HandlerHostGapUnit(host, host).handle(
        ModelHostGapUnitRequest(profile=profile, unit=unit, apply=apply)
    )


@pytest.fixture
def host_a() -> ModelHostProfile:
    return load_host("host-a")


def test_host_gap_unit_dry_run_is_the_default_and_changes_nothing(
    host_a: ModelHostProfile,
) -> None:
    host = FakeHost(bare(host_a))
    request = ModelHostGapUnitRequest(profile=host_a, unit=EnumGapUnit.G1_CLONES)
    assert request.apply is False
    result = HandlerHostGapUnit(host, host).handle(request)
    assert len(result.planned) == len(host_a.clones)
    assert result.done == ()
    assert host.applied == []


@pytest.mark.parametrize("unit", list(EnumGapUnit))
def test_host_gap_unit_apply_is_idempotent(
    host_a: ModelHostProfile, unit: EnumGapUnit
) -> None:
    seen = bare(host_a)
    if unit is EnumGapUnit.G4_VENVS:
        seen = seen.model_copy(
            update={
                "present_clone_paths": tuple(
                    c.path.replace("$OMNI_HOME", host_a.registry_root)
                    for c in host_a.clones
                )
            }
        )
    host = FakeHost(seen)
    first = run(host_a, host, unit, apply=True)
    assert first.planned
    assert first.done == first.planned
    second = run(host_a, host, unit, apply=True)
    assert second.planned == ()
    assert second.done == ()


def test_host_gap_unit_g1_clones_only_the_missing_repos(
    host_a: ModelHostProfile,
) -> None:
    have = tuple(
        c.path.replace("$OMNI_HOME", host_a.registry_root) for c in host_a.clones[:-1]
    )
    host = FakeHost(bare(host_a).model_copy(update={"present_clone_paths": have}))
    result = run(host_a, host, EnumGapUnit.G1_CLONES)
    assert [a.repo for a in result.planned] == [host_a.clones[-1].repo]
    assert result.planned[0].ref == host_a.clones[-1].ref


def test_host_gap_unit_g2_writes_the_rows_and_creates_the_workroot(
    host_a: ModelHostProfile,
) -> None:
    result = run(host_a, FakeHost(bare(host_a)), EnumGapUnit.G2_PREPUSH)
    kinds = [a.kind for a in result.planned]
    assert kinds == [EnumGapActionKind.MKDIR, *[EnumGapActionKind.WRITE] * 3]
    assert host_a.prepush is not None
    assert result.planned[0].path == host_a.prepush.workroot
    assert {a.path for a in result.planned[1:]} == set(prepush_target_files(host_a))


def test_host_gap_unit_g2_on_a_host_with_no_prepush_block_plans_nothing() -> None:
    h053 = load_host("host-b")
    assert run(h053, FakeHost(bare(h053)), EnumGapUnit.G2_PREPUSH).planned == ()


def test_host_gap_unit_g3_writes_every_liveness_row(host_a: ModelHostProfile) -> None:
    result = run(host_a, FakeHost(bare(host_a)), EnumGapUnit.G3_LIVENESS)
    assert [a.content for a in result.planned] == [liveness_rows_text(host_a)]


def test_host_gap_unit_g4_creates_venvs_only_for_present_clones_with_the_host_interpreter(
    host_a: ModelHostProfile,
) -> None:
    core = f"{host_a.registry_root}/core"
    host = FakeHost(bare(host_a).model_copy(update={"present_clone_paths": (core,)}))
    result = run(host_a, host, EnumGapUnit.G4_VENVS)
    assert [(a.path, a.python) for a in result.planned] == [
        (f"{core}/.venv", host_a.python)
    ]


def test_host_gap_unit_action_scripts_use_the_profile_values(
    host_a: ModelHostProfile,
) -> None:
    clone = ModelGapAction(
        unit=EnumGapUnit.G1_CLONES,
        kind=EnumGapActionKind.CLONE,
        path="/p/x",
        repo="o/x",
        ref="dev",
    )
    assert (
        action_script(host_a, clone)
        == "git clone --branch dev https://example.invalid/o/x.git /p/x\n"
    )
    venv = ModelGapAction(
        unit=EnumGapUnit.G4_VENVS,
        kind=EnumGapActionKind.VENV,
        path="/p/x/.venv",
        python="3.12",
    )
    assert action_script(host_a, venv) == "uv venv --python 3.12 /p/x/.venv\n"


@pytest.mark.parametrize("unit", list(EnumGapUnit))
def test_host_gap_unit_incomplete_measurement_refuses_apply(
    host_a: ModelHostProfile, unit: EnumGapUnit
) -> None:
    host = FakeHost(
        bare(host_a).model_copy(update={"errors": ("inventory unavailable",)})
    )
    with pytest.raises(ValueError, match="complete measurement"):
        run(host_a, host, unit, apply=True)
    assert host.applied == []
