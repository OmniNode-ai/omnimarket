# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Report every installed claimant of a host job that is not its owner as a duplicate.

For each job of the baseline, in order: every installed claimant that is not the owner is a
second unit; an owner unit whose ExecStart text does not contain the job's marker is a second
implementation. The read is pure: it writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_host_job_claim_compute.models import (
    EnumHostJobFindingKind,
    ModelHostJobClaimRequest,
    ModelHostJobClaimResult,
    ModelHostJobFinding,
)


class HandlerHostJobClaim:
    """Run the claim read of one host's installed units against its role baseline."""

    def handle(self, request: ModelHostJobClaimRequest) -> ModelHostJobClaimResult:
        installed = {unit.name: unit for unit in request.installed_units}
        findings: list[ModelHostJobFinding] = []
        for job in request.baseline.jobs:
            for claimant in sorted(job.claimants):
                if claimant != job.owner_unit and claimant in installed:
                    findings.append(
                        ModelHostJobFinding(
                            kind=EnumHostJobFindingKind.SECOND_UNIT,
                            job_id=job.job_id,
                            owner_unit=job.owner_unit,
                            extra_unit=claimant,
                        )
                    )
            owner = installed.get(job.owner_unit)
            if owner is not None and job.owner_command_marker not in owner.command:
                findings.append(
                    ModelHostJobFinding(
                        kind=EnumHostJobFindingKind.SECOND_IMPLEMENTATION,
                        job_id=job.job_id,
                        owner_unit=job.owner_unit,
                        extra_unit=job.owner_unit,
                    )
                )
        return ModelHostJobClaimResult(
            correlation_id=request.correlation_id,
            host=request.host,
            role=request.baseline.role,
            ok=not findings,
            findings=tuple(findings),
        )


def events_of(
    result: ModelHostJobClaimResult,
    topics: Mapping[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    """The events of a run: the duplicate finding, when any, then the run-completed event."""
    events: list[tuple[str, dict[str, Any]]] = []
    if result.findings:
        events.append(
            (
                topics["host_job_claim_duplicate_found"],
                {
                    "correlation_id": result.correlation_id,
                    "host": result.host,
                    "role": result.role,
                    "findings": [
                        {
                            "kind": finding.kind.value,
                            "job_id": finding.job_id,
                            "owner_unit": finding.owner_unit,
                            "extra_unit": finding.extra_unit,
                        }
                        for finding in result.findings
                    ],
                },
            )
        )
    events.append(
        (
            topics["host_job_claim_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "ok": result.ok,
                "findings": len(result.findings),
                "failed": 0 if result.ok else 1,
            },
        )
    )
    return events


def answer_command(
    payload: bytes,
    handler: HandlerHostJobClaim,
    topics: Mapping[str, str],
) -> list[tuple[str, bytes]]:
    """Answer one command: the run's events, or one failure event for a bad payload."""
    try:
        request = ModelHostJobClaimRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        )
        body = json.dumps(
            {"correlation_id": "unknown", "failure": f"bad-payload:{','.join(fields)}"},
            sort_keys=True,
        ).encode("utf-8")
        return [(topics["host_job_claim_run_failed"], body)]
    result = handler.handle(request)
    return [
        (topic, json.dumps(body, sort_keys=True).encode("utf-8"))
        for topic, body in events_of(result, topics)
    ]


__all__ = ["HandlerHostJobClaim", "answer_command", "events_of"]
