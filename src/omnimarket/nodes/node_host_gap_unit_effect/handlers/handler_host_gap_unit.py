# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four host gaps as installer units: plan from the profile, apply only when asked."""

from collections.abc import Callable

from omnimarket.handlers.handler_host_profile import (
    HandlerHostProfileCompute,
    liveness_rows_text,
    prepush_target_files,
)
from omnimarket.handlers.handler_host_shell import HandlerHostShell
from omnimarket.models.model_host_profile import (
    EnumGapActionKind,
    EnumGapUnit,
    ModelGapAction,
    ModelHostGapUnitRequest,
    ModelHostGapUnitResult,
    ModelHostMeasurement,
    ModelHostProfile,
    ModelHostProfileDeriveRequest,
)
from omnimarket.protocols.protocol_host_ports import (
    ProtocolHostActions,
    ProtocolHostMeasurement,
)


def plan_g1_clones(
    profile: ModelHostProfile, seen: ModelHostMeasurement
) -> list[ModelGapAction]:
    derived = HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )
    return [
        ModelGapAction(
            unit=EnumGapUnit.G1_CLONES,
            kind=EnumGapActionKind.CLONE,
            path=c.path,
            repo=c.repo,
            ref=c.ref,
        )
        for c in derived.clones
        if c.path not in seen.present_clone_paths
    ]


def plan_g2_prepush(
    profile: ModelHostProfile, seen: ModelHostMeasurement
) -> list[ModelGapAction]:
    if profile.prepush is None:
        return []
    actions: list[ModelGapAction] = []
    if not seen.workroot_present:
        actions.append(
            ModelGapAction(
                unit=EnumGapUnit.G2_PREPUSH,
                kind=EnumGapActionKind.MKDIR,
                path=profile.prepush.workroot,
            )
        )
    for target, line in prepush_target_files(profile).items():
        if line not in seen.prepush_rows.get(target, ()):
            actions.append(
                ModelGapAction(
                    unit=EnumGapUnit.G2_PREPUSH,
                    kind=EnumGapActionKind.WRITE,
                    path=target,
                    content="\n".join(
                        [
                            row
                            for row in seen.prepush_rows.get(target, ())
                            if row.split("\t", 1)[0] != profile.host
                        ]
                        + [line]
                    )
                    + "\n",
                )
            )
    return actions


def plan_g3_liveness(
    profile: ModelHostProfile, seen: ModelHostMeasurement
) -> list[ModelGapAction]:
    if profile.liveness is None:
        return []
    missing = [
        u for u in profile.liveness.units if u.native_id not in seen.liveness_native_ids
    ]
    if not missing:
        return []
    return [
        ModelGapAction(
            unit=EnumGapUnit.G3_LIVENESS,
            kind=EnumGapActionKind.WRITE,
            path=profile.resolve(profile.liveness.rows_target),
            content=liveness_rows_text(profile),
        )
    ]


def plan_g4_venvs(
    profile: ModelHostProfile, seen: ModelHostMeasurement
) -> list[ModelGapAction]:
    derived = HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )
    return [
        ModelGapAction(
            unit=EnumGapUnit.G4_VENVS,
            kind=EnumGapActionKind.VENV,
            path=v.venv_path,
            python=v.python,
        )
        for v in derived.venvs
        if v.venv_path not in seen.present_venvs
        and v.clone_path in seen.present_clone_paths
    ]


PLANNERS: dict[
    EnumGapUnit,
    Callable[[ModelHostProfile, ModelHostMeasurement], list[ModelGapAction]],
] = {
    EnumGapUnit.G1_CLONES: plan_g1_clones,
    EnumGapUnit.G2_PREPUSH: plan_g2_prepush,
    EnumGapUnit.G3_LIVENESS: plan_g3_liveness,
    EnumGapUnit.G4_VENVS: plan_g4_venvs,
}


class HandlerHostGapUnit:
    def __init__(
        self,
        measurement: ProtocolHostMeasurement | None = None,
        actions: ProtocolHostActions | None = None,
    ) -> None:
        adapter = HandlerHostShell()
        self._measurement = measurement if measurement is not None else adapter
        self._actions = actions if actions is not None else adapter

    def handle(self, request: ModelHostGapUnitRequest) -> ModelHostGapUnitResult:
        profile = request.profile
        seen = self._measurement.measure(profile)
        if seen.host != profile.host or seen.errors:
            raise ValueError(
                "installer requires a complete measurement of the requested host"
            )
        planned = PLANNERS[request.unit](profile, seen)
        done: list[ModelGapAction] = []
        if request.apply:
            for action in planned:
                self._actions.apply(profile, action)
                done.append(action)
        return ModelHostGapUnitResult(
            host=profile.host,
            unit=request.unit,
            applied=request.apply,
            planned=tuple(planned),
            done=tuple(done),
        )
