# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tart host admission (OMN-23204, plan M0): the credential route, its grade and the receipt.

Pure compute: no clock, file, network or environment read. A runner executes the planned
steps and supplies the evidence; this node decides. The ticket's acceptance falsifier is the
receipt rule: a host is admitted only when all of the host, the ``tart --version`` output, the
image digest and the seven probe results are present and read their admitting value, and the
probe is dated after the go.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_tart_host_admission_compute.models.model_host_admission import (
    EnumDeliveryStepKind,
    ModelAdmissionReceipt,
    ModelAdmissionRequest,
    ModelCredentialGrade,
    ModelCredentialGradeRequest,
    ModelCredentialPlan,
    ModelCredentialPlanRequest,
    ModelCredentialRouteSettings,
    ModelDeliveryStep,
)

#: The overlay id of the route's settings: <root>/<id>/overlay.yaml.
CREDENTIAL_ROUTE_OVERLAY_ID = "node_tart_host_admission_compute"

REASON_NOT_DELIVERED = "credential-not-delivered"
REASON_LENGTH_MISMATCH = "credential-length-mismatch"
REASON_NOT_ON_RAMDISK = "credential-not-on-ramdisk"
REASON_IN_IMAGE = "credential-in-image"
REASON_SCAN_BLIND = "disk-scan-blind"
REASON_IN_LOG = "credential-in-log"
REASON_EVIDENCE_MISSING = "credential-evidence-missing"

_SECTORS_PER_MEGABYTE = 2048
_REQUIRED_ANSWERS: Mapping[str, str] = {
    "guest_agent": "answered",
    "guest_capture": "answered",
    "lifecycle_observer": "answered",
    "lifecycle_controls": "passed",
    "probe_credentials": "reachable_not_in_image",
}


def load_credential_route_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelCredentialRouteSettings:
    """The route's settings from the first overlay root; none supplied is refused."""
    return load_structured_overlay(
        CREDENTIAL_ROUTE_OVERLAY_ID, ModelCredentialRouteSettings, environ
    )


class HandlerCredentialRoutePlan:
    """Plan the delivery of the probe credentials into a guest's RAM disk."""

    def handle(self, request: ModelCredentialPlanRequest) -> ModelCredentialPlan:
        s = request.settings
        mount = s.ramdisk_mount_point
        exec_ = (s.tart_path, "exec", request.vm_name)
        create = (
            "dev=$(/usr/bin/hdiutil attach -nomount ram://"
            f"{s.ramdisk_megabytes * _SECTORS_PER_MEGABYTE} | /usr/bin/awk '{{print $1}}') "
            f'&& /sbin/newfs_hfs -v {s.ramdisk_volume} "$dev" >/dev/null '
            f"&& sudo -n /bin/mkdir -p {mount} "
            f'&& sudo -n /sbin/mount -t hfs -o nobrowse "$dev" {mount} '
            f'&& sudo -n /usr/sbin/chown "$(id -u)" {mount}'
        )
        steps: list[ModelDeliveryStep] = [
            ModelDeliveryStep(
                kind=EnumDeliveryStepKind.RAMDISK_CREATE,
                argv=(*exec_, "sh", "-c", create),
            )
        ]
        for cred in s.credentials:
            target = f"{mount}/{cred.guest_file}"
            steps.append(
                ModelDeliveryStep(
                    kind=EnumDeliveryStepKind.CREDENTIAL_DELIVER,
                    argv=(
                        s.tart_path,
                        "exec",
                        "-i",
                        request.vm_name,
                        "sh",
                        "-c",
                        f"umask 077 && cat > {target}",
                    ),
                    stdin_credential=cred.name,
                )
            )
            steps.append(
                ModelDeliveryStep(
                    kind=EnumDeliveryStepKind.CREDENTIAL_LENGTH,
                    argv=(*exec_, "sh", "-c", f"wc -c < {target}"),
                )
            )
        steps.append(
            ModelDeliveryStep(
                kind=EnumDeliveryStepKind.RAMDISK_PROOF,
                argv=(
                    *exec_,
                    "sh",
                    "-c",
                    f'/usr/bin/hdiutil info | grep -c ram:// && /sbin/mount | grep -c "{mount} (hfs"',
                ),
            )
        )
        steps.append(
            ModelDeliveryStep(
                kind=EnumDeliveryStepKind.RAMDISK_DETACH,
                argv=(
                    *exec_,
                    "sh",
                    "-c",
                    (
                        f"dev=$(/sbin/mount | /usr/bin/awk 'index($3, \"{mount}\") {{print $1}}') "
                        '&& sudo -n /usr/bin/hdiutil detach "$dev" -force'
                    ),
                ),
            )
        )
        return ModelCredentialPlan(steps=tuple(steps), guest_mount=mount)


class HandlerCredentialRouteGrade:
    """Grade the route's evidence: reachable from the guest, and never written to the image."""

    def handle(self, request: ModelCredentialGradeRequest) -> ModelCredentialGrade:
        by_name = {e.name: e for e in request.evidence}
        reasons: set[str] = set()
        in_image = False
        for name in request.expected_names:
            ev = by_name.get(name)
            if ev is None:
                reasons.add(REASON_EVIDENCE_MISSING)
                continue
            if ev.delivered_exit_code != 0:
                reasons.add(REASON_NOT_DELIVERED)
            if ev.guest_length != ev.expected_length:
                reasons.add(REASON_LENGTH_MISMATCH)
            if not ev.on_ramdisk:
                reasons.add(REASON_NOT_ON_RAMDISK)
            if ev.disk_scan_hits > 0:
                reasons.add(REASON_IN_IMAGE)
                in_image = True
            if ev.disk_scan_control_hits < 1:
                reasons.add(REASON_SCAN_BLIND)
            if ev.value_in_walk_log:
                reasons.add(REASON_IN_LOG)
        ordered = tuple(sorted(reasons))
        if not reasons:
            verdict = "reachable_not_in_image"
        elif in_image or REASON_IN_LOG in reasons:
            verdict = "in_image"
        elif reasons <= {REASON_SCAN_BLIND}:
            verdict = "not_proven"
        else:
            verdict = "not_reachable"
        return ModelCredentialGrade(
            probe_credentials=verdict,
            reasons=ordered,
            status="passed" if not reasons else "failed",
        )


class HandlerHostAdmission:
    """The admission receipt: a host with any result missing or not admitting is not admitted."""

    def handle(self, request: ModelAdmissionRequest) -> ModelAdmissionReceipt:
        missing: list[str] = []
        failed: list[str] = []
        if request.tart_version is None:
            missing.append("tart_version")
        elif re.fullmatch(r"\d+\.\d+\.\d+\S*", request.tart_version.strip()) is None:
            failed.append("tart_version")
        if request.image_digest is None:
            missing.append("image_digest")
        elif re.fullmatch(r"sha256:[0-9a-f]{64}", request.image_digest) is None:
            failed.append("image_digest")
        for field, required in _REQUIRED_ANSWERS.items():
            value = getattr(request, field)
            if value is None:
                missing.append(field)
            elif value != required:
                failed.append(field)
        if request.vm_deleted is None:
            missing.append("vm_deleted")
        elif request.vm_deleted is not True:
            failed.append("vm_deleted")
        if request.probed_at <= request.go_at:
            failed.append("probed_before_go")
        admitted = not missing and not failed
        return ModelAdmissionReceipt(
            host=request.host,
            admitted=admitted,
            missing=tuple(missing),
            failed=tuple(failed),
            role="tart" if admitted else None,
            probed_at=request.probed_at,
            status="passed" if admitted else "failed",
        )
