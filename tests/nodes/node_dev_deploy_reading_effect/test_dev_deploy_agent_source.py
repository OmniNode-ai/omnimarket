# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 11's deploy-agent source against a real clone and the agent's own answers (OMN-23655).

Exemplar for shape: test_dev_deploy_lineage_facts.py (a real git fixture, injected ``fetch`` and ``run``). The answers are
shaped like the dev lane's deploy agent ``/health`` and ``/queue`` and the runtime's ``build-provenance.json`` read on
2026-11-11; the three readings (deployed, queued, failed) each come out of the same fixture with one answer changed, and the
unreadable halves read UNKNOWN, never a pass and never a zero.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_dev_deploy_reading_effect import (
    runtime_dev_deploy_reading as cli,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import (
    DeployAgentSource,
    LabDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters.adapter_lab_facts import (
    run_process,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_merge_probe import (
    HandlerDevDeployMergeProbe,
    ModelDevDeployMergeProbeCommand,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_reading_run import (
    HandlerDevDeployReadingRun,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployReadingCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_dev_deploy_reading_effect.dev_deploy_effect_support import (
    document,
    overlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
JOB_LAST = "11111111-1111-4111-8111-111111111111"
JOB_LIVE = "22222222-2222-4222-8222-222222222222"
HEALTH = "http://agent.test/health"
QUEUE = "http://agent.test/queue"
RUNTIME = "http://runtime.test/health"
ENVIRON = {"TEST_CLONE": "", "TEST_SSH_HOST": "lab@host.test"}


def _git(repo: str, *args: str, **extra_env: str) -> str:
    done = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(
            {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                **extra_env,
            }
        ),
    )
    return done.stdout.strip()


def _commit(repo: str, name: str, date: str) -> str:
    full = Path(repo) / "src" / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(name, encoding="utf-8")
    _git(repo, "add", f"src/{name}")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        name,
        GIT_AUTHOR_DATE=date,
        GIT_COMMITTER_DATE=date,
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> dict[str, str]:
    """A clone whose origin/dev holds two runtime-affecting commits, ``old`` (08:00) and ``new`` (10:00)."""
    repo = str(tmp_path)
    _git(repo, "init", "-q")
    old = _commit(repo, "old.py", "2026-11-08T08:00:00Z")
    new = _commit(repo, "new.py", "2026-11-08T10:00:00Z")
    _git(repo, "update-ref", "refs/remotes/origin/dev", new)
    return {"repo": repo, "old": old, "new": new}


def agent_answers(
    running: str,
    *,
    active: dict[str, Any] | None = None,
    last_status: str | None = "success",
    commands_ahead: Any = 0,
    queue_reason: str = "",
    build_time: str = "2026-11-08T09:00:00Z",
    section: str = "build_context",
    source_repo: str = "repo-a",
) -> dict[str, str | Exception]:
    last = (
        None
        if last_status is None
        else {
            "correlation_id": JOB_LAST,
            "status": last_status,
            "completed_at": "2026-11-08T09:20:00Z",
        }
    )
    provenance: dict[str, Any] = {"build_time": build_time, "vcs_ref": running}
    if section == "siblings":
        provenance["per_repo_vcs_provenance"] = {
            "siblings": {source_repo: {"vcs_ref": running}}
        }
    else:
        provenance["infra_vcs_ref"] = running
    return {
        HEALTH: json.dumps({"active_job": active, "last_result": last}),
        QUEUE: json.dumps(
            {
                "commands_ahead": commands_ahead,
                "control_topic_lag_reason": queue_reason,
            }
        ),
        RUNTIME: json.dumps({"details": {"healthy": True}}),
        "ssh": json.dumps(provenance),
    }


def fakes(
    answers: dict[str, str | Exception],
) -> tuple[Callable[[str, int], str], Callable[..., Any]]:
    def fetch(url: str, timeout: int) -> str:
        got = answers[url]
        if isinstance(got, Exception):
            raise got
        return got

    def run(argv: Sequence[str], stdin: bytes | None, timeout: int) -> Any:
        if argv[0] == "ssh":
            got = answers["ssh"]
            if isinstance(got, Exception):
                raise got
            return subprocess.CompletedProcess(list(argv), 0, got.encode(), b"")
        return run_process(argv, stdin, timeout)

    return fetch, run


def probe_merge(
    clone: dict[str, str],
    answers: dict[str, str | Exception],
    commit: str,
    repo: str = "repo-a",
    now: datetime = NOW,
):
    fetch, run = fakes(answers)
    environ = {**ENVIRON, "TEST_CLONE": clone["repo"]}
    return HandlerDevDeployMergeProbe(
        overlay(), environ, lambda: now, run=run, fetch=fetch
    ).handle(ModelDevDeployMergeProbeCommand(source_repo=repo, commit=commit))


IN_FLIGHT = {
    "correlation_id": JOB_LIVE,
    "current_phase": "runtime",
    "started_at": "2026-11-08T11:30:00Z",
}


# --- the three readings, against a real clone ------------------------------------------------------------------


def test_deployed_merge_contained_in_the_running_build_with_a_healthy_job_reads_working(
    clone: dict[str, str],
) -> None:
    got = probe_merge(clone, agent_answers(clone["new"]), clone["old"][:12])
    assert got.verdict is V.WORKING
    assert got.reasons == ()
    assert got.running_sha == clone["new"]
    assert got.last_job_id == JOB_LAST


def test_queued_merge_reads_unknown_with_its_queue_position_never_not_working(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["old"], active=IN_FLIGHT, commands_ahead=3)
    got = probe_merge(clone, answers, clone["new"][:12])
    assert got.verdict is V.UNKNOWN
    assert got.reasons == (f"deploy-queued:{clone['new'][:12]}:ahead-3",)
    assert got.commands_ahead == 3


def test_queued_control_an_idle_agent_past_its_grace_reads_not_working_for_the_same_merge(
    clone: dict[str, str],
) -> None:
    # Same fixture as the queued reading with the queue emptied and the agent idle: this is the edit that flips it.
    answers = agent_answers(clone["old"], active=None, commands_ahead=0)
    got = probe_merge(
        clone, answers, clone["new"][:12], now=datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
    )
    assert got.verdict is V.NOT_WORKING
    assert got.reasons[0].startswith(
        f"not-deployed:{clone['new'][:12]}:idle-agent-age-"
    )


def test_failed_roll_reads_not_working_with_the_job_id(clone: dict[str, str]) -> None:
    answers = agent_answers(clone["old"], last_status="failed")
    got = probe_merge(clone, answers, clone["new"][:12])
    assert got.verdict is V.NOT_WORKING
    assert got.reasons == (f"deploy-failed:{clone['new'][:12]}:job-{JOB_LAST}",)
    assert got.last_job_status == "failed"


def test_deployed_a_sibling_merge_reads_the_siblings_section_of_the_build_record(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["new"], section="siblings", source_repo="repo-b")
    got = probe_merge(clone, answers, clone["old"][:12], repo="repo-b")
    assert got.verdict is V.WORKING
    assert got.running_sha == clone["new"]


# --- error chain -----------------------------------------------------------------------------------------------


def test_error_chain_an_unreachable_agent_reads_unknown_and_names_why(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["new"])
    answers[HEALTH] = OSError("connection refused")
    got = probe_merge(clone, answers, clone["old"][:12])
    assert got.verdict is V.UNKNOWN
    assert got.reasons[0].startswith(
        "deploy-source-unread:deploy agent source unreadable: OSError"
    )


def test_error_chain_an_unread_queue_is_unknown_with_its_reason_and_never_zero(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(
        clone["old"],
        commands_ahead=None,
        queue_reason="lag sampler has not sampled",
    )
    got = probe_merge(clone, answers, clone["new"][:12])
    assert got.verdict is V.UNKNOWN
    assert got.reasons == (
        f"deploy-queue-unread:{clone['new'][:12]}:lag sampler has not sampled",
    )
    assert got.commands_ahead is None


def test_error_chain_an_unreadable_build_record_reads_unknown(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["new"])
    answers["ssh"] = "not json"
    got = probe_merge(clone, answers, clone["old"][:12])
    assert got.verdict is V.UNKNOWN
    assert got.reasons[0].startswith("deploy-source-unread:")


def test_error_chain_a_commit_outside_the_clone_or_an_unnamed_repo_is_refused(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["new"])
    missing = probe_merge(clone, answers, "deadbeef")
    assert missing.verdict is None
    assert missing.failure is not None
    other = probe_merge(clone, answers, clone["old"][:12], repo="repo-zzz")
    assert other.failure == "the overlay names no such merge repository"


def test_error_chain_a_queue_that_is_not_a_count_is_never_read_as_zero(
    clone: dict[str, str],
) -> None:
    answers = agent_answers(clone["old"], commands_ahead="many")
    fetch, run = fakes(answers)
    source = DeployAgentSource(
        overlay().deploy_agent, overlay().probe, ENVIRON, fetch, run
    )
    facts, reason = source.read_facts("build_context", "repo-a")
    assert reason is None
    assert facts is not None
    assert facts.commands_ahead is None
    assert (
        facts.commands_ahead_unread_reason
        == "queue snapshot has no commands_ahead count"
    )


# --- the hourly reading and its row ----------------------------------------------------------------------------


def hourly(
    clone: dict[str, str], answers: dict[str, str | Exception], ledger: InMemoryLedger
):
    fetch, run = fakes(answers)
    environ = {**ENVIRON, "TEST_CLONE": clone["repo"]}
    test_overlay = overlay(source_rule="deploy_agent")
    return HandlerDevDeployReadingRun(
        overlay=test_overlay,
        source=LabDevDeployFacts(test_overlay, environ, run=run, fetch=fetch),
        appender=ledger,
        clock=lambda: NOW,
        host="test-host",
    ).handle(ModelDevDeployReadingCommand(correlation_id="c-1", write=True))


def test_golden_chain_the_hourly_reading_with_every_merge_deployed_writes_a_working_row(
    clone: dict[str, str],
) -> None:
    ledger = InMemoryLedger()
    result = hourly(clone, agent_answers(clone["new"]), ledger)
    assert result.status == "completed"
    assert result.verdict is V.WORKING
    assert (result.merges_checked, result.merges_receipted) == (2, 2)
    assert result.row is not None
    assert "source_rule=deploy_agent" in result.row
    assert "verdict=WORKING" in result.row
    assert document()["source_rule"] == "lab_pass_receipt"


def test_error_chain_the_hourly_reading_with_a_queued_merge_writes_an_unknown_row_not_a_failure(
    clone: dict[str, str],
) -> None:
    ledger = InMemoryLedger()
    answers = agent_answers(clone["old"], active=IN_FLIGHT, commands_ahead=2)
    result = hourly(clone, answers, ledger)
    assert result.verdict is V.UNKNOWN
    assert result.reasons == (f"deploy-queued:{clone['new'][:12]}:ahead-2",)
    assert result.row is not None
    assert "verdict=UNKNOWN" in result.row
    assert f"deploy-queued:{clone['new'][:12]}:ahead-2" in result.row


def test_error_chain_the_hourly_reading_with_a_failed_roll_writes_not_working(
    clone: dict[str, str],
) -> None:
    ledger = InMemoryLedger()
    result = hourly(clone, agent_answers(clone["old"], last_status="failed"), ledger)
    assert result.verdict is V.NOT_WORKING
    # The old merge is in the running build but the roll that put it there failed; the new merge never got in.
    assert result.reasons == (
        f"deploy-failed:{clone['old'][:12]}:job-{JOB_LAST}",
        f"deploy-failed:{clone['new'][:12]}:job-{JOB_LAST}",
    )


# --- seams ------------------------------------------------------------------------------------------------------

#: Verbatim answers read from the dev lane's deploy agent on 2026-11-11 (port of the dev instance), trimmed of fields no reader uses.
LIVE_HEALTH = (
    '{"state": "deploying", "version": "0.1.0", "loaded_code_sha": "a9e64c8f5ef23b307b533d3e22e8a1703160d069",'
    ' "uptime_seconds": 130, "active_job": {"correlation_id": "c6ffdae0-80a3-421c-823e-daef825f5788",'
    ' "current_phase": "seed", "started_at": "2026-11-11T01:52:04.074038+00:00"},'
    ' "last_result": {"correlation_id": "059d1496-b0f3-41bc-b4bf-1499f2e4c07a", "status": "success",'
    ' "completed_at": "2026-11-11T01:47:53.163330+00:00", "phase_results": {"preflight": "success"},'
    ' "settling": false, "settling_stage": null}, "pending_publish_count": 0}'
)
LIVE_QUEUE = (
    '{"observed_at": "2026-11-11T01:53:37.403945+00:00", "in_flight_correlation_id": "c6ffdae0-80a3-421c-823e-daef825f5788",'
    ' "store_depth": 1, "control_topic_lag": 0, "control_topic_lag_reason": "",'
    ' "control_topic_lag_basis": "committed+end_offsets", "commands_ahead": 1, "mean_service_time_seconds": 1113.28}'
)
LIVE_PROVENANCE = (
    '{"build_source": "workspace", "build_time": "2026-11-11T01:04:26Z",'
    ' "infra_vcs_ref": "a21526125aef211060023631d08caad05016ff1c", "vcs_ref": "a21526125aef211060023631d08caad05016ff1c",'
    ' "per_repo_vcs_provenance": {"siblings": {"omnimarket": {"vcs_ref": "5964561a28f2ee540a2736657cc5d48e584e03a9"}}}}'
)


def test_seam_the_deploy_agents_own_answers_parse_into_facts() -> None:
    """Fails when the agent renames a field this reader takes from /health, /queue or the build record."""
    fetch, run = fakes(
        {
            HEALTH: LIVE_HEALTH,
            QUEUE: LIVE_QUEUE,
            RUNTIME: '{"details": {"healthy": true}}',
            "ssh": LIVE_PROVENANCE,
        }
    )
    source = DeployAgentSource(
        overlay().deploy_agent, overlay().probe, ENVIRON, fetch, run
    )
    facts, reason = source.read_facts("build_context", "repo-a")
    assert reason is None
    assert facts is not None
    assert facts.running_sha == "a21526125aef211060023631d08caad05016ff1c"
    assert facts.in_flight_job_id == "c6ffdae0-80a3-421c-823e-daef825f5788"
    assert facts.commands_ahead == 1
    assert facts.last_job_status == "success"
    sibling, reason = source.read_facts("siblings", "omnimarket")
    assert reason is None
    assert sibling is not None
    assert sibling.running_sha == "5964561a28f2ee540a2736657cc5d48e584e03a9"


def test_seam_the_request_and_the_effects_facts_carry_the_same_deploy_source_shape() -> (
    None
):
    """Fails when either side of the facts-to-request hand-over changes the deploy source's type."""
    from omnimarket.models.model_dev_deploy_reading import ModelDevDeployReadingRequest
    from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
        ModelDevDeployFacts,
    )

    assert (
        ModelDevDeployFacts.model_fields["deploy_source"].annotation
        == ModelDevDeployReadingRequest.model_fields["deploy_source"].annotation
    )


def test_seam_the_row_the_effect_writes_reads_unknown_through_the_reading_writers_rule(
    clone: dict[str, str],
) -> None:
    """Fails when the row's cells (src, drill, verdict) or the reading compute's unknown_when rule change shape."""
    from datetime import timedelta

    from omnimarket.models.model_reading import (
        ModelCellPredicate,
        ModelPieceReadingSpec,
        ModelReadingComputeRequest,
        ModelReadingSource,
        ModelReadingSourceRows,
    )
    from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
        HandlerReadingCompute,
    )
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        evidence_row_from_text,
    )

    ledger = InMemoryLedger()
    answers = agent_answers(clone["old"], active=IN_FLIGHT, commands_ahead=2)
    result = hourly(clone, answers, ledger)
    assert result.row is not None
    parsed = evidence_row_from_text(result.row, None)
    assert parsed is not None
    spec = ModelPieceReadingSpec(
        piece=11,
        name="lab deploy receipts",
        readable_today=True,
        sources=(
            ModelReadingSource(
                source_id="dev_deploy_reading",
                row_type="STATUS",
                lane="dev-deploy-reading",
                cells={"src": "dev-deploy-reading", "drill": "no"},
                mode="newest",
                unknown_when=(
                    ModelCellPredicate(cell="verdict", op="eq", value="UNKNOWN"),
                ),
                pass_when=(
                    ModelCellPredicate(cell="verdict", op="eq", value="WORKING"),
                ),
                max_age_seconds=7200,
            ),
        ),
        injected_fault="No PASS receipt within an hour",
    )
    end = parsed.stamp + timedelta(minutes=5)
    reading = HandlerReadingCompute().handle(
        ModelReadingComputeRequest(
            spec=spec,
            window_start=end - timedelta(hours=1),
            window_end=end,
            cutoff=end + timedelta(minutes=10),
            order_hash="o",
            overlay_hash="h",
            revision="1.0.0",
            sources=(
                ModelReadingSourceRows(
                    source_id="dev_deploy_reading", rows=(parsed,), error=None
                ),
            ),
        )
    )
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == ("source-unknown:dev_deploy_reading",)


# --- AC4: the probe-merge verb itself, end to end over the agent's answers -------------------------------------


def _run_probe_merge_verb(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clone: dict[str, str],
    answers: dict[str, str | Exception],
    shas: Sequence[str],
) -> tuple[int, list[dict[str, Any]]]:
    """Run ``main(["probe-merge", ...])`` as the command line does; only the overlay file and the two network reads are injected."""
    fetch, run = fakes(answers)
    monkeypatch.setenv("TEST_CLONE", clone["repo"])
    monkeypatch.setenv("TEST_SSH_HOST", "lab@host.test")
    monkeypatch.setattr(cli, "load_overlay", overlay)
    monkeypatch.setattr(
        cli,
        "HandlerDevDeployMergeProbe",
        lambda ov, environ, _clock: HandlerDevDeployMergeProbe(
            ov, environ, lambda: NOW, run=run, fetch=fetch
        ),
    )
    args = ["probe-merge", "--repo", "repo-a"]
    for sha in shas:
        args += ["--sha", sha]
    code = cli.main(args)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    return code, lines


def test_probe_merge_verb_prints_one_verdict_per_sha_and_exits_zero(
    clone: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = agent_answers(clone["old"], active=IN_FLIGHT, commands_ahead=2)
    code, lines = _run_probe_merge_verb(
        monkeypatch, capsys, clone, answers, [clone["old"][:12], clone["new"][:12]]
    )
    assert code == 0
    assert [x["commit"] for x in lines] == [clone["old"][:12], clone["new"][:12]]
    assert [x["verdict"] for x in lines] == ["WORKING", "UNKNOWN"]
    assert lines[1]["reasons"] == [f"deploy-queued:{clone['new'][:12]}:ahead-2"]


def test_error_chain_probe_merge_verb_exits_non_zero_when_the_deploy_agent_is_unreachable(
    clone: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = agent_answers(clone["new"])
    answers[HEALTH] = OSError("connection refused")
    code, lines = _run_probe_merge_verb(
        monkeypatch, capsys, clone, answers, [clone["old"][:12]]
    )
    assert code == 3
    assert lines[0]["verdict"] == "UNKNOWN"
    assert lines[0]["reasons"][0].startswith("deploy-source-unread:")


def test_error_chain_probe_merge_verb_exits_one_for_a_commit_outside_the_clone(
    clone: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, lines = _run_probe_merge_verb(
        monkeypatch, capsys, clone, agent_answers(clone["new"]), ["deadbeef"]
    )
    assert code == 1
    assert lines[0]["failure"]
