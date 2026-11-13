# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler tests of the host job claim read.

The handler is pure: it reads the installed units against the role baseline and names every
installed claimant of a job that is not its owner as a duplicate and the owner that lost its
marker as a second implementation. A control that skips the check misses the duplicate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_job_claim_compute.handlers.handler_host_job_claim import (
    HandlerHostJobClaim,
)
from omnimarket.nodes.node_host_job_claim_compute.models import (
    EnumHostJobFindingKind,
    ModelHostJob,
    ModelHostJobClaimRequest,
    ModelHostRoleBaseline,
    ModelInstalledUnit,
)

pytestmark = pytest.mark.unit

OWNER = "onex-lane-reaper"
SECOND = "onex-old-reaper"
OTHER = "onex-other"
JOB = "reap-lane"
MARKER = "lane_reaper_node"


def _baseline() -> ModelHostRoleBaseline:
    return ModelHostRoleBaseline(
        role="lane-reaper",
        jobs=(
            ModelHostJob(
                job_id=JOB,
                owner_unit=OWNER,
                owner_command_marker=MARKER,
                claimants=(OWNER, SECOND),
            ),
        ),
    )


def _request(installed: tuple[ModelInstalledUnit, ...]) -> ModelHostJobClaimRequest:
    return ModelHostJobClaimRequest(
        correlation_id="corr-1",
        host="host-a",
        installed_units=installed,
        baseline=_baseline(),
    )


def test_baseline_duplicate_is_one_second_unit_finding() -> None:
    installed = (
        ModelInstalledUnit(name=OWNER, command=f"/bin/{MARKER}"),
        ModelInstalledUnit(name=SECOND, command="/bin/old"),
        ModelInstalledUnit(name=OTHER, command="/bin/other"),
    )
    result = HandlerHostJobClaim().handle(_request(installed))
    assert result.ok is False
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind is EnumHostJobFindingKind.SECOND_UNIT
    assert finding.job_id == JOB
    assert finding.owner_unit == OWNER
    assert finding.extra_unit == SECOND


def test_owner_without_marker_is_one_second_implementation_finding() -> None:
    installed = (ModelInstalledUnit(name=OWNER, command="/bin/other-marker"),)
    result = HandlerHostJobClaim().handle(_request(installed))
    assert result.ok is False
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind is EnumHostJobFindingKind.SECOND_IMPLEMENTATION
    assert finding.job_id == JOB
    assert finding.owner_unit == OWNER
    assert finding.extra_unit == OWNER


def test_installed_only_the_owner_is_ok() -> None:
    installed = (ModelInstalledUnit(name=OWNER, command=f"/bin/{MARKER}"),)
    result = HandlerHostJobClaim().handle(_request(installed))
    assert result.ok is True
    assert result.findings == ()


def control_handler_without_check(request: ModelHostJobClaimRequest):
    """The control that misses the duplicate: it returns ok unconditionally."""
    from omnimarket.nodes.node_host_job_claim_compute.models import (
        ModelHostJobClaimResult,
    )

    return ModelHostJobClaimResult(
        correlation_id=request.correlation_id,
        host=request.host,
        role=request.baseline.role,
        ok=True,
        findings=(),
    )


def test_control_misses_the_duplicate_while_the_real_handler_does_not() -> None:
    installed = (
        ModelInstalledUnit(name=OWNER, command=f"/bin/{MARKER}"),
        ModelInstalledUnit(name=SECOND, command="/bin/old"),
        ModelInstalledUnit(name=OTHER, command="/bin/other"),
    )
    request = _request(installed)
    control = control_handler_without_check(request)
    real = HandlerHostJobClaim().handle(request)
    assert control.ok is True
    assert control.findings == ()
    assert real.ok is False
    assert len(real.findings) == 1


def test_a_unit_claiming_two_jobs_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ModelHostRoleBaseline(
            role="lane-reaper",
            jobs=(
                ModelHostJob(
                    job_id="job-a",
                    owner_unit=OWNER,
                    owner_command_marker=MARKER,
                    claimants=(OWNER, SECOND),
                ),
                ModelHostJob(
                    job_id="job-b",
                    owner_unit=OTHER,
                    owner_command_marker="other-marker",
                    claimants=(OTHER, SECOND),
                ),
            ),
        )


def test_a_claimants_list_missing_the_owner_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ModelHostJob(
            job_id=JOB,
            owner_unit=OWNER,
            owner_command_marker=MARKER,
            claimants=(SECOND,),
        )


def test_duplicate_job_ids_raise_validation_error() -> None:
    with pytest.raises(ValidationError):
        ModelHostRoleBaseline(
            role="lane-reaper",
            jobs=(
                ModelHostJob(
                    job_id=JOB,
                    owner_unit=OWNER,
                    owner_command_marker=MARKER,
                    claimants=(OWNER,),
                ),
                ModelHostJob(
                    job_id=JOB,
                    owner_unit=OTHER,
                    owner_command_marker="other-marker",
                    claimants=(OTHER,),
                ),
            ),
        )
