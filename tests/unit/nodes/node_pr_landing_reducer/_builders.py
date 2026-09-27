# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders that turn the symbolic counterexample fixtures into typed models.

tests/fixtures/pr_landing/counterexamples.yaml names heads symbolically (h1,
h2), gives the ordering key as ``seq`` and leaves out the fields a case does
not care about. These builders fill the rest with fixed values, so every
reducer test is deterministic and reads no clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingArmMethod,
    EnumPrLandingCompanionOutcome,
    EnumPrLandingCompanionStatus,
    EnumPrLandingIntentKind,
    EnumPrLandingObservationKind,
    EnumPrLandingState,
    ModelPrLandingCheckAttempt,
    ModelPrLandingCompanion,
    ModelPrLandingIntent,
    ModelPrLandingObservation,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    HEAD_CHECK_RERUN_VERDICTS,
    EnumHeadCheckVerdict,
)

REPOSITORY = "OmniNode-ai/omnimarket"
PR_NUMBER = 4242
OCC_PR = 9001
TICKETS = ("OMN-19828",)
T0 = datetime(2026, 9, 27, 7, 0, tzinfo=UTC)

_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = _ROOT / "tests" / "fixtures" / "pr_landing"


def head(symbol: str) -> str:
    """``h1`` -> a 40-hex sha that is stable per symbol."""
    index = int(symbol.removeprefix("h"))
    return f"{index:x}" * 40 if index < 16 else f"{index:040x}"


def load_yaml(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast("dict[str, Any]", loaded)


def reducer_cases() -> list[dict[str, Any]]:
    cases = load_yaml("counterexamples.yaml")["cases"]
    return [c for c in cases if c.get("scope") != "orchestrator"]


def intent(
    kind: EnumPrLandingIntentKind, head_sha: str | None, **fields: Any
) -> ModelPrLandingIntent:
    return ModelPrLandingIntent(
        kind=kind,
        repository=REPOSITORY,
        pr_number=PR_NUMBER,
        head_sha=head_sha,
        **fields,
    )


def _companion(spec: dict[str, Any] | None) -> ModelPrLandingCompanion:
    if not spec:
        return ModelPrLandingCompanion()
    status = EnumPrLandingCompanionStatus(spec["status"])
    unbound = (
        EnumPrLandingCompanionStatus.NONE,
        EnumPrLandingCompanionStatus.PENDING,
        EnumPrLandingCompanionStatus.DECLINED,
    )
    return ModelPrLandingCompanion(
        status=status,
        command_id=spec.get("command_id"),
        occ_pr=None if status in unbound else OCC_PR,
    )


def start_row(start: dict[str, Any]) -> ModelPrLandingState:
    """The row a case starts from; unnamed fields take neutral values."""
    head_sha = head(start["head"]) if "head" in start else None
    armed = start.get("armed")
    outbox = tuple(
        intent(EnumPrLandingIntentKind(kind), head_sha)
        for kind in start.get("outbox", [])
    )
    return ModelPrLandingState(
        repository=REPOSITORY,
        pr_number=PR_NUMBER,
        state=EnumPrLandingState(start["state"]),
        head_sha=head_sha,
        base_ref="dev",
        ticket_ids=TICKETS,
        draft=bool(start.get("draft", False)),
        held=bool(start.get("held", False)),
        source_seq=int(start.get("seq", 0)),
        companion=_companion(start.get("companion")),
        armed=EnumPrLandingArmMethod(armed) if armed else None,
        expected_attempts=tuple(
            ModelPrLandingCheckAttempt(check=check, attempt=attempt)
            for check, attempt in (start.get("expected_attempts") or {}).items()
        ),
        episode=int(start.get("episode", 0)),
        state_entry_generation=int(start.get("state_entry_generation", 0)),
        outbox=outbox,
        seq=10,
        entered_state_at=T0,
        landing_key=f"{REPOSITORY}#{PR_NUMBER}",
    )


def observation(spec: dict[str, Any], step: int) -> ModelPrLandingObservation:
    """One symbolic fixture observation as a typed observation."""
    kind = EnumPrLandingObservationKind(spec["kind"])
    fields: dict[str, Any] = {
        "repository": REPOSITORY,
        "pr_number": PR_NUMBER,
        "kind": kind,
        "observed_at": T0 + timedelta(minutes=step + 1),
        "source_topic": "test.pr-landing",
        "source_event_id": f"step-{step}",
    }
    if "head" in spec:
        fields["head_sha"] = head(spec["head"])
    if "seq" in spec:
        fields["source_seq"] = spec["seq"]
    for flag in ("draft", "held"):
        if flag in spec:
            fields[flag] = spec[flag]
    if kind is EnumPrLandingObservationKind.HEAD_CHECKS:
        verdict = EnumHeadCheckVerdict(spec["verdict"])
        attempts = spec.get("check_attempts") or {}
        fields["verdict"] = verdict
        fields["arm_method"] = EnumPrLandingArmMethod.AUTO_MERGE
        fields["check_attempts"] = tuple(
            ModelPrLandingCheckAttempt(check=c, attempt=a) for c, a in attempts.items()
        )
        if verdict in HEAD_CHECK_RERUN_VERDICTS:
            fields["rerun_checks"] = tuple(attempts) or ("ci",)
    if kind is EnumPrLandingObservationKind.COMPANION_OUTCOME:
        outcome = EnumPrLandingCompanionOutcome(spec["outcome"].lower())
        fields["command_id"] = spec["command_id"]
        fields["companion_outcome"] = outcome
        if outcome is EnumPrLandingCompanionOutcome.MINTED:
            fields["occ_pr"] = OCC_PR
            fields["companion_stamped"] = True
            fields["companion_armed"] = True
    if kind in (
        EnumPrLandingObservationKind.COMPANION_MERGED,
        EnumPrLandingObservationKind.COMPANION_CONFLICTING,
        EnumPrLandingObservationKind.COMPANION_CLOSED,
    ):
        fields["occ_pr"] = OCC_PR
    if kind is EnumPrLandingObservationKind.BOUND_EXPIRED:
        fields["episode"] = spec["episode"]
        fields["state_entry_generation"] = spec["state_entry_generation"]
    if kind is EnumPrLandingObservationKind.EVALUATION:
        fields["companion_required"] = bool(spec.get("companion_required", False))
        fields["base_served"] = bool(spec.get("base_served", True))
    return ModelPrLandingObservation.model_validate(fields)


__all__: list[str] = [
    "FIXTURES",
    "OCC_PR",
    "PR_NUMBER",
    "REPOSITORY",
    "T0",
    "TICKETS",
    "head",
    "intent",
    "load_yaml",
    "observation",
    "reducer_cases",
    "start_row",
]
