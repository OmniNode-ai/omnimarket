# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""host_drift_check: anything undeclared or missing fails; a matching host passes."""

from pathlib import Path

from omnimarket.handlers.handler_host_profile import (
    HandlerHostProfileCompute,
    liveness_rows_text,
    parse_host_profile,
    prepush_target_files,
)
from omnimarket.handlers.handler_host_shell import (
    HandlerHostShell,
    measure_script,
    parse_measurement,
)
from omnimarket.models.model_host_profile import (
    EnumDriftKind,
    ModelHostDriftCheckRequest,
    ModelHostDriftCheckResult,
    ModelHostMeasurement,
    ModelHostProfile,
    ModelHostProfileDeriveRequest,
)
from omnimarket.nodes.node_host_drift_check_effect.handlers.handler_host_drift_check import (
    HandlerHostDriftCheck,
)

EXAMPLE_PROFILES = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_host_profile_compute/host_profiles.example.yaml"
)


def load_host(host: str) -> ModelHostProfile:
    return parse_host_profile(EXAMPLE_PROFILES.read_text(), host)


class FakeMeasurement:
    def __init__(self, seen: ModelHostMeasurement) -> None:
        self.seen = seen

    def measure(self, profile: ModelHostProfile) -> ModelHostMeasurement:
        return self.seen


def matching(profile: ModelHostProfile) -> ModelHostMeasurement:
    derived = HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )
    registry = profile.registry_root
    return ModelHostMeasurement(
        host=profile.host,
        git_dirs_in_registry=tuple(
            c.path.removeprefix(f"{registry}/")
            for c in derived.clones
            if c.path.startswith(f"{registry}/")
            and c.path.count("/") == registry.count("/") + 1
        ),
        present_clone_paths=tuple(c.path for c in derived.clones),
        present_venvs=tuple(v.venv_path for v in derived.venvs),
        workroot_present=True,
        prepush_rows={t: (line,) for t, line in prepush_target_files(profile).items()},
        liveness_native_ids=tuple(
            u.native_id for u in (profile.liveness.units if profile.liveness else ())
        ),
        units=derived.systemd_units,
    )


def check(
    profile: ModelHostProfile, seen: ModelHostMeasurement
) -> ModelHostDriftCheckResult:
    return HandlerHostDriftCheck(FakeMeasurement(seen)).handle(
        ModelHostDriftCheckRequest(profile=profile)
    )


def kinds(result: ModelHostDriftCheckResult) -> set[tuple[EnumDriftKind, str]]:
    return {(f.kind, f.subject) for f in result.findings}


def test_host_drift_check_matching_host_passes() -> None:
    profile = load_host("host-a")
    result = check(profile, matching(profile))
    assert result.ok
    assert result.findings == ()


def test_host_drift_check_extra_clone_fails() -> None:
    profile = load_host("host-a")
    seen = matching(profile)
    seen = seen.model_copy(
        update={"git_dirs_in_registry": (*seen.git_dirs_in_registry, "stray")}
    )
    result = check(profile, seen)
    assert not result.ok
    assert kinds(result) == {(EnumDriftKind.UNDECLARED_CLONE, "stray")}


def test_host_drift_check_missing_clone_fails() -> None:
    profile = load_host("host-a")
    seen = matching(profile)
    gone = "/srv/registry/graph"
    seen = seen.model_copy(
        update={
            "git_dirs_in_registry": tuple(
                n for n in seen.git_dirs_in_registry if n != "graph"
            ),
            "present_clone_paths": tuple(
                p for p in seen.present_clone_paths if p != gone
            ),
        }
    )
    result = check(profile, seen)
    assert not result.ok
    assert kinds(result) == {(EnumDriftKind.MISSING_CLONE, gone)}


def test_host_drift_check_missing_venv_workroot_row_and_units_fail() -> None:
    profile = load_host("host-a")
    seen = matching(profile).model_copy(
        update={
            "present_venvs": (),
            "workroot_present": False,
            "prepush_rows": {},
            "liveness_native_ids": (),
            "units": ("onex-stray.service",),
        }
    )
    found = {k for k, _ in kinds(check(profile, seen))}
    assert found == {
        EnumDriftKind.MISSING_VENV,
        EnumDriftKind.MISSING_WORKROOT,
        EnumDriftKind.MISSING_PREPUSH_ROW,
        EnumDriftKind.MISSING_LIVENESS_ROW,
        EnumDriftKind.MISSING_UNIT,
        EnumDriftKind.UNDECLARED_UNIT,
    }


def test_host_drift_check_shell_port_parses_what_the_script_prints() -> None:
    profile = load_host("host-a")
    target = next(iter(prepush_target_files(profile)))
    output = (
        "G\tmarket\nC\t/srv/registry/market\n"
        "V\t/srv/registry/market/.venv\nW\n"
        f"P\t{target}\ta\tb\nL\tonex-reconcile.timer\nU\tonex-x.timer\n"
    )
    seen = parse_measurement(profile, output)
    assert seen.git_dirs_in_registry == ("market",)
    assert seen.workroot_present
    assert seen.prepush_rows == {target: ("a\tb",)}
    assert seen.liveness_native_ids == ("onex-reconcile.timer",)
    assert seen.units == ("onex-x.timer",)


def test_host_drift_check_shell_port_runs_the_script_through_the_injected_runner() -> (
    None
):
    profile = load_host("host-a")
    ran: list[tuple[str, str]] = []

    def runner(p: ModelHostProfile, script: str) -> str:
        ran.append((p.host, script))
        return "G\tmarket\n"

    seen = HandlerHostShell(runner).measure(profile)
    assert ran == [("host-a", measure_script(profile))]
    assert seen.git_dirs_in_registry == ("market",)


def test_host_drift_check_liveness_rows_text_names_every_declared_unit() -> None:
    profile = load_host("host-a")
    text = liveness_rows_text(profile)
    assert profile.liveness is not None
    for unit in profile.liveness.units:
        assert f"native_id: {unit.native_id}" in text


def test_host_drift_check_unreadable_units_never_report_in_sync() -> None:
    profile = load_host("host-b").model_copy(
        update={
            "clones": (),
            "liveness": None,
            "systemd_units": ("onex-unknown.service",),
        }
    )
    seen = parse_measurement(profile, "E\tuser unit inventory unavailable\n")
    result = check(profile, seen)
    assert not result.ok
    assert [(f.kind.value, f.subject) for f in result.findings] == [
        ("measurement_unavailable", "user unit inventory unavailable")
    ]
