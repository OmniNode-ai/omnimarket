# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, check and launch only from the immutable dispatch instruction record.

The runner's existing CLI remains the transport edge. Its flags are derived here,
not supplied by a sender. The unchanged lane brief checker remains the final
authority on generated standing rules, reader output and live ticket ownership.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from omnimarket.enums.enum_lab_job import EnumLabJobKind
from omnimarket.models.lab_job.model_lab_job_spec import ModelLabJobSpec
from omnimarket.models.lane_dispatch.model_lane_dispatch import (
    ModelLaneDispatchRecord,
    ModelLaneDispatchSpec,
)

from ..models.model_lane_dispatch_effect import (
    ModelLaneDispatchOverlay,
    ModelLaneDispatchRequest,
    ModelLaneDispatchResult,
)
from ..overlay import load_dispatch_overlay


class ProtocolLaneDispatchCommand(Protocol):
    """Host command adapter; argv is never passed through a shell."""

    def run(self, argv: Sequence[str], timeout_s: int) -> tuple[int, str]: ...


class LocalLaneDispatchCommand:
    """The checker and runner on the dispatching host."""

    def run(self, argv: Sequence[str], timeout_s: int) -> tuple[int, str]:
        done = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout_s
        )
        return done.returncode, (done.stdout + done.stderr).strip()


def render_brief(record: ModelLaneDispatchRecord, record_path: Path) -> str:
    """Preserve reader-generated shell text, with all task metadata from the spec."""
    spec = record.spec
    premise_rows = "\n".join(
        f"| {premise.claim} | verified by: {premise.evidence} |"
        for premise in spec.premises
    )
    ack = f"\nticket-state-ack={spec.ticket_state_ack}" if spec.ticket_state_ack else ""
    constraints = spec.constraints
    if spec.supersede:
        import re

        # The unchanged checker reads these cells in the 3a.8 section only.
        constraints = re.sub(
            r"(?ms)(^### 3a\.8\b[^\n]*\n.*?)(?=^### |\Z)",
            lambda match: (
                match.group(1).rstrip()
                + "\n\n"
                + "\n".join(
                    f"supersedes-claim={claim.stamp}" for claim in spec.supersede
                )
                + "\n\n"
            ),
            constraints,
        )
    steps = ", ".join(spec.publish_steps) or "none"
    return (
        f"# {spec.title}\n\nLane: {spec.lane}\nLane kind: {spec.lane_kind}\n"
        f"Model: {spec.model}. Effort: {spec.effort}.\n"
        f"Required host: {spec.host}\nPlacement: {spec.placement}\n"
        f"Repo: {spec.repo}\nRef: {spec.ref}\nTicket: {spec.ticket}\nParent: {spec.parent}\n"
        f"PRs: {', '.join(spec.pr)}\nPriority class: {spec.priority_class}\n"
        f"Memory limit bytes: {spec.memory_limit_bytes}\n"
        f"Publish steps: {steps}\nHandoff: {spec.handoff.mode} actor={spec.handoff.actor}\n"
        f"Dispatch record: {record_path}\nDispatch sha256: {record.content_hash}\n"
        f"Job: {spec.job_id}{ack}\n\n"
        "## 1. Premise table\n\n| premise | verified by |\n| -- | -- |\n"
        f"{premise_rows}\n\n## 2. Task\n\n{spec.task}\n\n"
        f"## 3. Rules\n\n{spec.rules}\n\n{constraints}\n\n"
        f"## 4. Report contract\n\n{spec.report}\n"
    )


def runner_arguments(spec: ModelLaneDispatchSpec, brief_path: Path) -> tuple[str, ...]:
    """Existing runner protocol. Sender-controlled argv and host overrides do not exist."""
    owner, repo = spec.repo.split("/", 1)
    argv = [
        "run",
        "--detach",
        "--no-placement-wait",
        "--brief",
        str(brief_path),
        "--lane",
        spec.lane,
        "--owner",
        owner,
        "--repo",
        repo,
        "--sha",
        spec.ref,
        "--ticket",
        spec.ticket,
        "--parent",
        spec.parent,
        "--placement",
        spec.placement,
        "--host",
        spec.host,
        "--model",
        spec.model,
        "--priority-class",
        spec.priority_class,
        "--mem-max",
        str(spec.memory_limit_bytes),
        "--timeout-min",
        str(spec.time_box_min),
        "--job-id",
        spec.job_id,
        "--max-attempts",
        str(spec.retry_policy.max_attempts),
        "--on-time-box",
        spec.retry_policy.on_time_box.value,
    ]
    if spec.model in spec.policy.runner_effort_models:
        argv.extend(("--effort", spec.effort))
    for pr in spec.pr:
        argv.extend(("--pr", pr))
    for flag, value in (
        ("--risk-class", spec.risk_class),
        ("--escalation-reason", spec.escalation_reason),
        ("--max-turns", spec.max_turns),
    ):
        if value is not None:
            argv.extend((flag, str(value)))
    for flag, values in (
        ("--writable", spec.writable),
        ("--check", spec.checks),
        ("--context", spec.context),
    ):
        for value in values:
            argv.extend((flag, value))
    for criterion in spec.done_criteria:
        value = criterion.kind.value
        if criterion.target:
            # The supervisor uses owner/repo#n; the runner accepts repo#n.
            value += ":" + criterion.target.rsplit("/", 1)[-1]
        if criterion.check_context:
            value += ":" + criterion.check_context
        argv.extend(("--done", value))
    return tuple(argv)


def supervisor_spec(spec: ModelLaneDispatchSpec, brief: str) -> ModelLabJobSpec:
    """Reuse the supervisor's existing schema; this projection is never the job record."""
    return ModelLabJobSpec(
        job_id=spec.job_id,
        kind=EnumLabJobKind.LANE,
        brief=brief,
        repo=spec.repo,
        ref=spec.ref,
        engine=spec.model,
        host_preference=(spec.host,),
        time_box_min=spec.time_box_min,
        retry_policy=spec.retry_policy,
        done_criteria=spec.done_criteria,
        parent_lane=spec.parent,
        ticket=spec.ticket,
    )


class HandlerLaneDispatch:
    """The canonical dispatch effect; injected ports exercise the production wiring."""

    def __init__(
        self,
        overlay: ModelLaneDispatchOverlay | None = None,
        command: ProtocolLaneDispatchCommand | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._command = command or LocalLaneDispatchCommand()
        self._now = now or (lambda: datetime.now(UTC))

    def handle(self, request: ModelLaneDispatchRequest) -> ModelLaneDispatchResult:
        """Fail closed: a rejected checker never yields or executes a runner command."""
        try:
            overlay = self._overlay or load_dispatch_overlay()
            raw = json.loads(request.record_json)
            if isinstance(raw, dict) and ("spec" in raw or "content_hash" in raw):
                # An envelope must verify; never repair or rehash a damaged record.
                record = ModelLaneDispatchRecord.model_validate(raw)
            else:
                # A dispatcher may write just spec data. The canonical effect supplies
                # its declared policy, validates the build, and stamps the job record.
                if isinstance(raw, dict):
                    raw = {"policy": overlay.policy, **raw}
                record = ModelLaneDispatchRecord.from_spec(
                    ModelLaneDispatchSpec.model_validate(raw)
                )
            spec = record.spec
            if spec.policy != overlay.policy:
                return ModelLaneDispatchResult(
                    status="refused",
                    error_code="DISPATCH-POLICY-MISMATCH",
                    detail="record policy differs from the authoritative overlay",
                )
            age = (self._now() - spec.facts.read_at).total_seconds()
            if age < 0 or age > overlay.policy.snapshot_max_age_s:
                return ModelLaneDispatchResult(
                    status="refused",
                    error_code="DISPATCH-SNAPSHOT-STALE",
                    detail="read fresh ticket, watcher and claim facts before dispatch",
                )
            output_dir = Path(request.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            record_path = output_dir / f"{record.content_hash}.job.json"
            brief_path = output_dir / f"{record.content_hash}.brief.md"
            brief = render_brief(record, record_path)
            # Validate the supervisor projection before either external command.
            supervisor_spec(spec, brief)
            record_path.write_text(record.canonical_json() + "\n", encoding="utf-8")
            brief_path.write_text(brief, encoding="utf-8")
            code, detail = self._command.run(
                (
                    *overlay.checker_command,
                    "check",
                    str(brief_path),
                    "--lane",
                    spec.lane,
                ),
                overlay.command_timeout_s,
            )
            if code != 0:
                return ModelLaneDispatchResult(
                    status="refused",
                    job_id=spec.job_id,
                    content_hash=record.content_hash,
                    record_path=str(record_path),
                    brief_path=str(brief_path),
                    error_code="LANE-BRIEF-REFUSED",
                    detail=detail,
                )
            argv = (*overlay.runner_command, *runner_arguments(spec, brief_path))
            runner_exit: int | None = None
            if request.execute:
                runner_exit, detail = self._command.run(argv, overlay.command_timeout_s)
                if runner_exit != 0:
                    return ModelLaneDispatchResult(
                        status="refused",
                        job_id=spec.job_id,
                        content_hash=record.content_hash,
                        record_path=str(record_path),
                        brief_path=str(brief_path),
                        error_code="REMOTE-LANE-RUNNER-REFUSED",
                        detail=detail,
                        runner_exit=runner_exit,
                    )
            return ModelLaneDispatchResult(
                status="submitted" if request.execute else "checked",
                job_id=spec.job_id,
                content_hash=record.content_hash,
                record_path=str(record_path),
                brief_path=str(brief_path),
                brief=brief,
                runner_argv=argv,
                runner_exit=runner_exit,
            )
        except ValidationError as exc:
            return ModelLaneDispatchResult(
                status="refused",
                error_code=str(exc.errors()[0]["type"]),
                detail=str(exc),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return ModelLaneDispatchResult(
                status="refused", error_code="DISPATCH-UNREADABLE", detail=str(exc)
            )
