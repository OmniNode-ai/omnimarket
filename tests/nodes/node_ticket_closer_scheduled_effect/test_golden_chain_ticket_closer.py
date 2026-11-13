# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and failing-control chains of the scheduled ticket closer (OMN-24138)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    ModelProofCloseRequest,
)
from omnimarket.models.ticket_closer import (
    EnumCloserAction,
    ModelCloserCandidate,
    ModelCloserVerifyReading,
    ModelCloserWriteReceipt,
    ModelTicketCloserScheduleOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloseoutAction,
    ModelDodCloseoutDecisionResult,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers import (
    handler_ticket_closer_scheduled as closer_module,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.handler_ticket_closer_scheduled import (
    HandlerTicketCloserScheduled,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_ticket_closer_scheduled_effect/overlay.yaml"
)
HANDLER_SOURCE = Path(closer_module.__file__)
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def schedule() -> ModelTicketCloserScheduleOverlay:
    return ModelTicketCloserScheduleOverlay.model_validate(
        yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    )


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=closer@example.invalid",
            "-c",
            "user.name=closer",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@dataclass(frozen=True)
class Repo:
    path: Path
    root: str
    change: str
    later: str


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    path = tmp_path / "product"
    path.mkdir()
    _git(path, "init", "-q", "-b", "dev")
    shas: list[str] = []
    for name in ("root", "change", "later"):
        (path / f"{name}.txt").write_text(name, encoding="utf-8")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", name)
        shas.append(_git(path, "rev-parse", "HEAD"))
    return Repo(path=path, root=shas[0], change=shas[1], later=shas[2])


def candidate(
    repo: Repo,
    *,
    ticket_id: str = "OMN-1",
    criteria: tuple[str, ...] = ("AC1", "AC2"),
    all_prs_merged: bool = True,
    with_proof: bool = True,
) -> ModelCloserCandidate:
    return ModelCloserCandidate(
        ticket_id=ticket_id,
        criteria=criteria,
        all_prs_merged=all_prs_merged,
        runtime_proof=(
            ModelProofCloseRequest(
                ticket_id=ticket_id,
                change_merged=True,
                change_sha=repo.change,
                repo_path=str(repo.path),
            )
            if with_proof
            else None
        ),
    )


def passing(
    repo: Repo, ticket_id: str = "OMN-1", **over: object
) -> ModelCloserVerifyReading:
    fields: dict[str, object] = {
        "ticket_id": ticket_id,
        "ran": True,
        "total_checks": 2,
        "verified_count": 2,
        "behavior_proving_count": 1,
        "bound_criteria": ("AC1", "AC2"),
        "head_revision": repo.later,
        "exercises_running_system": True,
    }
    fields.update(over)
    return ModelCloserVerifyReading.model_validate(fields)


@dataclass
class Source:
    found: list[ModelCloserCandidate] | None
    calls: int = 0
    limits: list[int] = field(default_factory=list)

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None:
        self.calls += 1
        self.limits.append(limit)
        return self.found


@dataclass
class Verifier:
    readings: dict[str, ModelCloserVerifyReading]
    raises: Exception | None = None

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading:
        if self.raises is not None:
            raise self.raises
        return self.readings[ticket_id]


@dataclass
class Writer:
    receipt: ModelCloserWriteReceipt = field(
        default_factory=lambda: ModelCloserWriteReceipt(ok=True)
    )
    raises: Exception | None = None
    calls: list[dict[str, str]] = field(default_factory=list)

    def mark_done(
        self, ticket_id: str, *, state: str, lane: str, evidence: str
    ) -> ModelCloserWriteReceipt:
        self.calls.append(
            {"ticket_id": ticket_id, "state": state, "lane": lane, "evidence": evidence}
        )
        if self.raises is not None:
            raise self.raises
        return self.receipt


def handler(
    source: Source, verifier: Verifier, writer: Writer
) -> HandlerTicketCloserScheduled:
    return HandlerTicketCloserScheduled(
        schedule=schedule(), source=source, verifier=verifier, writer=writer
    )


def run_one(
    repo: Repo,
    cand: ModelCloserCandidate,
    reading: ModelCloserVerifyReading,
    writer: Writer | None = None,
) -> tuple[object, Writer]:
    out = writer or Writer()
    result = handler(Source([cand]), Verifier({cand.ticket_id: reading}), out).handle(
        tick(T0)
    )
    assert result is not None
    return result, out


def test_golden_a_proven_ticket_is_moved_to_done_with_its_evidence(repo: Repo) -> None:
    result, writer = run_one(repo, candidate(repo), passing(repo))
    assert result.candidates_read is True
    assert result.examined == 1
    assert result.flipped == 1
    assert result.outcomes[0].action is EnumCloserAction.FLIPPED_DONE
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["ticket_id"] == "OMN-1"
    assert call["state"] == "Done"
    assert call["lane"] == "ticket-closer-fixture"
    assert repo.later in call["evidence"]


@pytest.mark.parametrize(
    ("name", "cand", "reading"),
    [
        (
            "an_unbound_criterion",
            {},
            {"bound_criteria": ("AC1",)},
        ),
        (
            "no_runtime_proof_supplied",
            {"with_proof": False},
            {},
        ),
        (
            "a_receipt_revision_that_lacks_the_change",
            {},
            {"head_revision": "ROOT"},
        ),
        (
            "an_unmerged_change",
            {"all_prs_merged": False},
            {},
        ),
        (
            "no_behaviour_proving_check",
            {},
            {"behavior_proving_count": 0},
        ),
        (
            "a_verifier_run_that_does_not_exercise_the_running_system",
            {},
            {"exercises_running_system": False},
        ),
    ],
)
def test_control_each_missing_conjunct_holds_the_ticket(
    repo: Repo, name: str, cand: dict[str, object], reading: dict[str, object]
) -> None:
    over = {
        key: (repo.root if value == "ROOT" else value) for key, value in reading.items()
    }
    result, writer = run_one(
        repo,
        candidate(repo, **cand),
        passing(repo, **over),  # type: ignore[arg-type]
    )
    assert result.flipped == 0, name
    assert result.outcomes[0].action is EnumCloserAction.HELD, name
    assert result.outcomes[0].reason or result.outcomes[0].unmet, name
    assert writer.calls == [], name


def test_control_no_runtime_proof_says_merged_not_proven_running(repo: Repo) -> None:
    result, _ = run_one(repo, candidate(repo, with_proof=False), passing(repo))
    assert "merged, not yet proven running" in result.outcomes[0].reason


def test_control_a_loosened_rule_flips_the_unproven_ticket(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected break: with the predicate loosened to always flip, the unproven ticket is written.

    The control tests above fail for real: they hold only because the rule holds.
    """
    monkeypatch.setattr(
        closer_module,
        "flip_decision",
        lambda request: ModelDodCloseoutDecisionResult(
            kind=request.kind,
            action=EnumCloseoutAction.FLIPPED_DONE,
            predicate_met=True,
            unmet=[],
            primary_blocker=None,
            unbound_criteria=[],
        ),
    )
    result, writer = run_one(repo, candidate(repo, with_proof=False), passing(repo))
    assert result.flipped == 1
    assert len(writer.calls) == 1


def test_the_rule_is_the_existing_predicate() -> None:
    text = HANDLER_SOURCE.read_text(encoding="utf-8")
    assert "node_dod_closeout_sweep_compute" in text
    assert "import (\n    flip_decision,\n)" in text or "import flip_decision" in text
    assert "behavior_proving_count == 0" not in text
    assert "BLOCK_" not in text


def test_error_a_verifier_that_cannot_run_judges_nothing(repo: Repo) -> None:
    reading = ModelCloserVerifyReading(
        ticket_id="OMN-1",
        ran=False,
        error_code="VERIFY_COULD_NOT_RUN",
        error_detail="contract unresolved",
    )
    result, writer = run_one(repo, candidate(repo), reading)
    assert result.flipped == 0
    assert result.outcomes[0].action is EnumCloserAction.VERIFY_NOT_RUN
    assert "VERIFY_COULD_NOT_RUN" in result.outcomes[0].reason
    assert writer.calls == []


def test_error_a_verifier_that_raises_is_reported_not_swallowed(repo: Repo) -> None:
    out = Writer()
    result = handler(
        Source([candidate(repo)]), Verifier({}, raises=RuntimeError("boom")), out
    ).handle(tick(T0))
    assert result is not None
    assert result.outcomes[0].action is EnumCloserAction.VERIFY_NOT_RUN
    assert "RuntimeError" in result.outcomes[0].reason
    assert out.calls == []


def test_error_a_refused_write_leaves_the_ticket_open_and_says_why(repo: Repo) -> None:
    writer = Writer(receipt=ModelCloserWriteReceipt(ok=False, reason="guard refused"))
    result, _ = run_one(repo, candidate(repo), passing(repo), writer)
    assert result.flipped == 0
    assert result.outcomes[0].action is EnumCloserAction.WRITE_REFUSED
    assert result.outcomes[0].reason == "guard refused"
    assert len(writer.calls) == 1


def test_error_a_writer_that_raises_is_reported_not_swallowed(repo: Repo) -> None:
    writer = Writer(raises=TimeoutError("no terminal"))
    result, _ = run_one(repo, candidate(repo), passing(repo), writer)
    assert result.flipped == 0
    assert result.outcomes[0].action is EnumCloserAction.WRITE_REFUSED
    assert "TimeoutError" in result.outcomes[0].reason


def test_error_an_unread_source_is_unread_never_an_empty_success() -> None:
    result = handler(Source(None), Verifier({}), Writer()).handle(tick(T0))
    assert result is not None
    assert result.candidates_read is False
    assert result.examined == 0
    assert result.outcomes == ()


def test_the_schedule_gate_runs_once_per_interval(repo: Repo) -> None:
    source = Source([candidate(repo)])
    h = handler(source, Verifier({"OMN-1": passing(repo)}), Writer())
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=59))) is None
    assert source.calls == 1
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None
    assert source.calls == 2


def test_error_a_skipped_tick_does_not_move_the_last_run(repo: Repo) -> None:
    h = handler(Source([]), Verifier({}), Writer())
    h.handle(tick(T0))
    assert h.handle(tick(T0 + timedelta(seconds=30))) is None
    assert h.handle(tick(T0 + timedelta(seconds=60))) is not None


def test_error_a_missing_overlay_refuses_without_acting(repo: Repo) -> None:
    source = Source([candidate(repo)])
    h = HandlerTicketCloserScheduled(
        source=source,
        verifier=Verifier({}),
        writer=Writer(),
        environ={},
    )
    assert h.handle(tick(T0)) is None
    assert source.calls == 0


def test_the_candidate_limit_is_the_overlays(repo: Repo) -> None:
    cands = [candidate(repo, ticket_id=f"OMN-{n}") for n in range(1, 6)]
    readings = {c.ticket_id: passing(repo, c.ticket_id) for c in cands}
    source = Source(cands)
    result = handler(source, Verifier(readings), Writer()).handle(tick(T0))
    assert result is not None
    assert source.limits == [3]
    assert result.examined == 3
    assert result.flipped == 3
