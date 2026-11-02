# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare a host's measured state to its profile; anything undeclared or missing is drift."""

from omnimarket.handlers.handler_host_profile import (
    HandlerHostProfileCompute,
    liveness_row,
    prepush_target_files,
)
from omnimarket.handlers.handler_host_shell import HandlerHostShell
from omnimarket.models.model_host_profile import (
    EnumDriftKind,
    ModelDriftFinding,
    ModelHostDriftCheckRequest,
    ModelHostDriftCheckResult,
    ModelHostProfileDeriveRequest,
)
from omnimarket.protocols.protocol_host_ports import ProtocolHostMeasurement


class HandlerHostDriftCheck:
    def __init__(self, measurement: ProtocolHostMeasurement | None = None) -> None:
        self._measurement = (
            measurement if measurement is not None else HandlerHostShell()
        )

    def handle(self, request: ModelHostDriftCheckRequest) -> ModelHostDriftCheckResult:
        profile = request.profile
        derived = HandlerHostProfileCompute().handle(
            ModelHostProfileDeriveRequest(profile=profile)
        )
        seen = self._measurement.measure(profile)
        findings = [
            ModelDriftFinding(kind=EnumDriftKind.MEASUREMENT_UNAVAILABLE, subject=error)
            for error in seen.errors
        ]
        if seen.host != profile.host:
            raise ValueError("measurement host does not match the requested profile")

        registry = profile.registry_root.rstrip("/")
        declared_names = {
            c.path.removeprefix(f"{registry}/")
            for c in derived.clones
            if c.path.startswith(f"{registry}/")
            and "/" not in c.path.removeprefix(f"{registry}/")
        }
        for name in sorted(set(seen.git_dirs_in_registry) - declared_names):
            findings.append(
                ModelDriftFinding(kind=EnumDriftKind.UNDECLARED_CLONE, subject=name)
            )
        for clone in derived.clones:
            if clone.path not in seen.present_clone_paths:
                findings.append(
                    ModelDriftFinding(
                        kind=EnumDriftKind.MISSING_CLONE, subject=clone.path
                    )
                )
        for venv in derived.venvs:
            if venv.venv_path not in seen.present_venvs:
                findings.append(
                    ModelDriftFinding(
                        kind=EnumDriftKind.MISSING_VENV, subject=venv.venv_path
                    )
                )
        if derived.prepush_workroot is not None and not seen.workroot_present:
            findings.append(
                ModelDriftFinding(
                    kind=EnumDriftKind.MISSING_WORKROOT,
                    subject=derived.prepush_workroot,
                )
            )
        for target, line in prepush_target_files(profile).items():
            if line not in seen.prepush_rows.get(target, ()):
                findings.append(
                    ModelDriftFinding(
                        kind=EnumDriftKind.MISSING_PREPUSH_ROW, subject=target
                    )
                )
        for unit in profile.liveness.units if profile.liveness else ():
            if unit.native_id not in seen.liveness_native_ids:
                findings.append(
                    ModelDriftFinding(
                        kind=EnumDriftKind.MISSING_LIVENESS_ROW,
                        subject=str(liveness_row(profile.host, unit)["process_id"]),
                    )
                )
        if not seen.errors:
            declared_units = set(derived.systemd_units)
            for name in derived.systemd_units:
                if name not in seen.units:
                    findings.append(
                        ModelDriftFinding(kind=EnumDriftKind.MISSING_UNIT, subject=name)
                    )
            for name in sorted(set(seen.units) - declared_units):
                findings.append(
                    ModelDriftFinding(kind=EnumDriftKind.UNDECLARED_UNIT, subject=name)
                )

        return ModelHostDriftCheckResult(
            host=profile.host,
            ok=not findings,
            findings=tuple(findings),
            measurement=seen,
        )
