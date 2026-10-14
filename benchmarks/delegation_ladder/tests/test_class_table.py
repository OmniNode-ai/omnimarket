# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class tables count scorer passes separately from routing acceptance."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from benchmarks.delegation_ladder import run_ladder as runner
from benchmarks.delegation_ladder import summarise_results as tables
from benchmarks.delegation_ladder.models import (
    EnumDifficulty,
    EnumOutcome,
    EnumPath,
    ModelRunRecord,
    ModelTaskBundle,
    is_accepted,
)

pytestmark = pytest.mark.unit
HERE = Path(__file__).resolve().parent


def bundle(task_id: str = "task") -> ModelTaskBundle:
    return ModelTaskBundle(
        task_id=task_id,
        rung="R3",
        title="Read",
        task_type="reasoning",
        provenance="unit control",
        prompt="Return the held answer.",
        scorer="exact_match",
        scorer_config={"accepted": ["held value"]},
        difficulty=EnumDifficulty.EASY,
    )


def receipt() -> dict[str, Any]:
    return json.loads((HERE / "fixtures" / "deployed_receipt.json").read_text())


@pytest.mark.parametrize(
    ("k", "n", "expected"),
    [
        (0, 10, (0.0, 0.2775)),
        (5, 10, (0.2366, 0.7634)),
        (10, 10, (0.7225, 1.0)),
        (0, 0, (0.0, 1.0)),
    ],
)
def test_wilson_interval(k: int, n: int, expected: tuple[float, float]) -> None:
    assert tables.wilson_interval(k, n) == pytest.approx(expected, abs=1e-3)
    if n:
        assert tables.wilson_interval(k, n) != (k / n, k / n)


@pytest.mark.parametrize("gate", [True, False, None])
def test_acceptance_requires_both_scorer_and_gate(gate: bool | None) -> None:
    assert is_accepted(EnumOutcome.PASS, gate) is (gate is not False)
    for outcome in (EnumOutcome.FAIL, EnumOutcome.REFUSED, EnumOutcome.HARNESS_ERROR):
        assert not is_accepted(outcome, gate)


def test_old_records_and_bundles_keep_loading() -> None:
    old = json.loads((runner.RESULTS / "merged-local.json").read_text())["records"][0]
    record = ModelRunRecord.model_validate(old)
    assert record.backend is None
    assert record.attempts == []
    assert record.accepted is None
    legacy = bundle().model_dump(exclude={"difficulty"})
    assert ModelTaskBundle.model_validate(legacy).difficulty is None
    with pytest.raises(ValueError):
        ModelTaskBundle.model_validate({**legacy, "difficulty": "extreme"})


def test_deployed_receipt_preserves_evidence_and_failed_status() -> None:
    got = runner.parse_deployed_receipt(receipt())
    assert got.response == "held value"
    assert not got.refused
    assert got.model_id == "Qwen3.8-27B"
    assert got.backend_id == "local-coder"
    assert got.host == "lab-test"
    assert got.cost_usd == 0.0
    assert got.model_latency_ms == 123
    assert got.input_tokens == 10
    assert got.output_tokens == 2
    assert got.total_tokens == 12
    assert got.quality_gate_passed is True
    assert got.quality_score == 0.9
    bad = receipt()
    bad["result"]["terminal_payload"]["payload"].update(
        status="failed", error_message="no answer"
    )
    failed = runner.parse_deployed_receipt(bad)
    assert failed.response == ""
    assert not failed.refused
    assert "failed" in failed.notes
    assert "no answer" in failed.notes
    assert runner.parse_deployed_receipt({}).response == ""
    empty = receipt()
    empty["result"]["terminal_payload"]["payload"]["response"] = ""
    assert runner.parse_deployed_receipt(empty).response == ""


def unpinned_receipt() -> dict[str, Any]:
    raw = receipt()
    payload = raw["result"]["terminal_payload"]["payload"]
    payload["attempts"].insert(
        0,
        {
            "backend_id": "first",
            "tier": "local",
            "model_id": "small",
            "quality_gate_passed": False,
            "acceptance_decision": "refuse",
            "acceptance_reason": "below gate",
            "failure_class": "quality",
            "error_message": "x" * 250,
            "host": "lab-first",
        },
    )
    payload["escalation_count"] = 1
    return raw


def test_unpinned_attempts_keep_refusals() -> None:
    got = runner.parse_deployed_receipt(unpinned_receipt())
    assert len(got.attempts) == 2
    assert got.escalation_count == 1
    assert got.host == "lab-test"
    assert got.attempts[0]["acceptance_reason"] == "below gate"
    assert len(got.attempts[0]["error_message"]) == 200
    assert runner.parse_deployed_receipt(receipt()).escalation_count == 0


@pytest.mark.parametrize("backend", ["local-coder", "local-omnipc2-chat", "unpinned"])
def test_onex_command_pins_only_when_requested(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        env = kwargs["env"]
        assert env["ONEX_DELEGATION_ALLOW_PAID"] == "0"
        assert argv[argv.index("--ticket") + 1] == "OMN-20844"
        assert "--json" in argv
        assert kwargs["check"] is False
        if backend == "local-omnipc2-chat":
            assert argv[argv.index("--bus") + 1] == "inmemory"
            assert env["BIFROST_OVERLAY_PATH"] == str(
                Path.home() / ".omninode/delegation/bifrost_overrides.pc2.yaml"
            )
            assert "--lane" not in argv
            assert argv[argv.index("--locus") + 1] == "in-process"
        else:
            assert argv[argv.index("--bus") + 1] == "kafka"
            assert argv[argv.index("--lane") + 1] == "dev"
            assert argv[argv.index("--locus") + 1] == "deployed-lane"
            assert argv[argv.index("--state-root") + 1] == str(tmp_path / ".onex_state")
        if backend == "unpinned":
            assert "--backend-id" not in argv
        else:
            # The overlay repoints local-coder itself at the second lab host.
            pinned = "local-coder" if backend == "local-omnipc2-chat" else backend
            assert argv[argv.index("--backend-id") + 1] == pinned
        return subprocess.CompletedProcess(
            argv, 0, "human line\n" + json.dumps(receipt()) + "\n", ""
        )

    got = runner.invoke_pinned(
        bundle(),
        runner.BACKENDS[backend],
        onex=tmp_path / "onex",
        timeout_s=3,
        run=fake_run,
    )
    assert got.response == "held value"
    assert not got.refused
    assert len(calls) == 1


def test_harness_reads_receipt_and_requires_plugin_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_LANE", "unit-lane")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == [
            "python3",
            str(tmp_path / "skills/harness-delegate/scripts/harness_delegate.py"),
            "run",
        ]
        assert argv[argv.index("--harness") + 1] == "claude-glm"
        assert argv[argv.index("--model") + 1] == "glm-5.3-flash"
        assert argv[argv.index("--task-class") + 1] == "reasoning"
        assert argv[argv.index("--lane") + 1] == "unit-lane"
        assert argv[argv.index("--check") + 1] == 'test -s "$HARNESS_ANSWER_FILE"'
        assert (
            Path(argv[argv.index("--prompt-file") + 1]).read_text() == bundle().prompt
        )
        work = Path(argv[argv.index("--out-dir") + 1]) / "unit-run"
        work.mkdir()
        answer = work / "answer.txt"
        answer.write_text("held value")
        (work / "receipt.json").write_text(
            json.dumps(
                {
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                    "duration_s": 0.5,
                    "outcome": "pass",
                    "timed_out": False,
                    "error": None,
                    "answer_file": str(answer),
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, "unit-run\n", "")

    got = runner.invoke_pinned(
        bundle(),
        runner.BACKENDS["glm-5.3-flash"],
        onex=tmp_path / "onex",
        timeout_s=3,
        run=fake_run,
    )
    assert got.response == "held value"
    assert got.input_tokens == 5
    assert got.output_tokens == 2
    assert got.cost_usd is None
    assert got.quality_gate_passed is None
    assert got.model_latency_ms == 500
    monkeypatch.delenv("OMNI_PLUGIN_ROOT")
    with pytest.raises(RuntimeError, match="OMNI_PLUGIN_ROOT"):
        runner.invoke_pinned(
            bundle(),
            runner.BACKENDS["glm-5.3-flash"],
            onex=tmp_path / "onex",
            timeout_s=3,
            run=fake_run,
        )


@pytest.mark.parametrize("timeout", [False, True])
def test_transport_failure_is_not_retried(
    timeout: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    calls = 0

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if timeout:
            raise subprocess.TimeoutExpired(argv, 3, stderr="timed out detail")
        return subprocess.CompletedProcess(argv, 1, "no JSON", "transport error")

    got = runner.invoke_pinned(
        bundle(),
        runner.BACKENDS["local-coder"],
        onex=tmp_path / "onex",
        timeout_s=3,
        run=fake_run,
    )
    assert got.refused
    assert got.response == ""
    assert calls == 1
    assert ("timed out detail" if timeout else "transport error") in got.notes


def records(backend: str, passed: int, n: int = 25) -> list[dict[str, Any]]:
    return [
        {
            "task_id": str(i),
            "task_type": "reasoning",
            "difficulty": "easy",
            "backend": backend,
            "accepted": i < passed,
            "score": {"outcome": "pass" if i < passed else "fail"},
            "quality_gate_passed": None,
            "model_latency_ms": None,
            "wall_ms": 1000 + i * 100,
            "cost_usd": None,
        }
        for i in range(n)
    ]


def test_class_intervals_separate_only_disjoint_backends() -> None:
    rows = tables.class_rows(records("a", 25) + records("b", 10))
    assert rows[0]["n"] == 25
    assert rows[0]["accepted"] == 25
    assert rows[0]["share"] == 1.0
    assert rows[0]["median_wall_s"] == 2.2
    assert rows[0]["cost_usd"] is None
    assert rows[0]["cost_receipts"] == 0
    assert rows[0]["median_model_latency_ms"] is None
    separated = tables.separation(rows)["reasoning"]
    assert "a" in separated["separated"]
    assert separated["disjoint_pairs"] == [("a", "b")]
    overlap = tables.separation(tables.class_rows(records("a", 20) + records("b", 21)))[
        "reasoning"
    ]
    assert overlap["separated"] == []
    assert overlap["verdict"] == "none separated"
    assert overlap["overlapping_pairs"] == [("a", "b")]
    three = tables.separation(
        tables.class_rows(records("a", 25) + records("b", 10) + records("c", 10))
    )["reasoning"]
    assert three["separated"] == ["a"]


def test_class_rows_separate_gate_refusals_and_sum_only_receipts() -> None:
    raw = records("a", 4, 6)
    raw[0].update(accepted=False, quality_gate_passed=False, cost_usd=0.0)
    raw[1].update(cost_usd=0.125, model_latency_ms=100)
    raw[2].update(cost_usd=0.25, model_latency_ms=200)
    raw[5]["score"]["outcome"] = "refused"
    row = tables.class_rows(raw)[0]
    assert (
        row["accepted"],
        row["scorer_pass"],
        row["gate_refused"],
        row["failed"],
        row["refused"],
    ) == (3, 4, 1, 1, 1)
    assert row["cost_usd"] == 0.375
    assert row["cost_receipts"] == 3
    assert row["median_model_latency_ms"] == 150
    legacy = copy.deepcopy(raw)
    for record in legacy:
        record.pop("accepted")
    assert tables.class_rows(legacy)[0]["accepted"] == 3
    assert tables.class_rows([]) == []


def test_first_failing_tier_uses_difficulty_order() -> None:
    raw = records("a", 10, 10)
    raw += [{**r, "difficulty": "medium"} for r in records("a", 9, 10)]
    raw += [{**r, "difficulty": "hard"} for r in records("a", 5, 10)]
    rows = tables.tier_rows(raw)
    assert tables.first_failing_tier(list(reversed(rows)), 0.8) == {
        ("reasoning", "a"): "hard"
    }
    assert tables.first_failing_tier([{**r, "share": 0.8} for r in rows]) == {
        ("reasoning", "a"): None
    }
    assert tables.first_failing_tier(rows, 0.95) == {("reasoning", "a"): "medium"}


def test_jsonl_precedence_resume_and_overwrite_guard(tmp_path: Path) -> None:
    first, later = tmp_path / "first.jsonl", tmp_path / "later.jsonl"
    raw = records("a", 2, 2)
    raw.append(
        {**raw[0], "task_id": "retry", "notes": "harness_error: missing receipt"}
    )
    first.write_text("".join(json.dumps(r) + "\n" for r in raw))
    later.write_text(
        json.dumps({**raw[0], "accepted": False})
        + "\n"
        + json.dumps({**raw[0], "backend": "b"})
        + "\n"
    )
    merged = tables.load_jsonl([first, later])
    assert len(merged) == 4
    assert merged[0]["accepted"] is False
    done = runner.completed_task_ids(first)
    pending = runner.remaining_bundles(
        [bundle("0"), bundle("1"), bundle("retry"), bundle("new")], done
    )
    assert [b.task_id for b in pending] == ["retry", "new"]
    assert runner.remaining_bundles([bundle("0")], set()) == [bundle("0")]
    with pytest.raises(FileExistsError, match=r"first\.jsonl"):
        runner.check_results_file(first, resume=False)
    runner.check_results_file(first, resume=True)
    runner.check_results_file(tmp_path / "absent.jsonl", resume=False)


def test_escalation_rows_do_not_hide_refused_attempts() -> None:
    got = runner.parse_deployed_receipt(unpinned_receipt())
    raw = {
        "backend": "unpinned",
        "task_id": "task",
        "attempts": got.attempts,
        "escalation_count": 1,
        "wall_ms": 1234,
    }
    row = tables.escalation_rows([raw])[0]
    assert row["answering_backend"] == "local-coder"
    assert row["escalation_count"] == 1
    assert row["refused_attempts"] == ["first:refuse:below gate"]
    assert row["wall_ms"] == 1234
    assert tables.escalation_rows([{**raw, "backend": "local-coder"}]) == []
    assert (
        tables.escalation_rows([{**raw, "attempts": got.attempts[:1]}])[0][
            "answering_backend"
        ]
        == "none"
    )


@pytest.mark.parametrize("backend", ["local-coder", "unpinned"])
def test_run_one_records_acceptance_and_host(
    backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    got = runner.parse_deployed_receipt(unpinned_receipt())
    monkeypatch.setattr(runner, "invoke_pinned", lambda *_args, **_kwargs: got)
    path = EnumPath.UNPINNED if backend == "unpinned" else EnumPath.PINNED
    record = runner.run_one(
        bundle(),
        path,
        tmp_path / "onex",
        None,
        3,
        backend=backend,
        transcript_dir=tmp_path,
    )
    assert record.accepted is True
    assert record.backend == backend
    assert record.task_type == "reasoning"
    assert record.difficulty == EnumDifficulty.EASY
    assert record.host == ("lab-test" if backend == "unpinned" else "qwen-host-a")
    assert len(record.attempts) == 2
    assert record.escalation_count == 1
    bad = receipt()
    bad["result"]["terminal_payload"]["payload"]["quality_gate_passed"] = False
    monkeypatch.setattr(
        runner,
        "invoke_pinned",
        lambda *_args, **_kwargs: runner.parse_deployed_receipt(bad),
    )
    assert (
        runner.run_one(
            bundle(), path, tmp_path / "onex", None, 3, backend=backend
        ).accepted
        is False
    )


def test_runner_cli_appends_and_resumes_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    onex = home / "omnibase_infra/scripts/onex"
    onex.parent.mkdir(parents=True)
    onex.touch()
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.setattr(runner, "RESULTS", tmp_path / "committed-results")
    source = tmp_path / "bundles"
    tiered = source / "tiered"
    tiered.mkdir(parents=True)
    for task_id in ("0", "1", "retry"):
        (tiered / f"{task_id}.json").write_text(bundle(task_id).model_dump_json())
    (tiered / "other.json").write_text(
        bundle("other").model_copy(update={"task_type": "document"}).model_dump_json()
    )
    (tiered / "hard.json").write_text(
        bundle("hard")
        .model_copy(update={"difficulty": EnumDifficulty.HARD})
        .model_dump_json()
    )
    monkeypatch.setattr(runner, "BUNDLES", source)
    out = tmp_path / "results" / "custom.jsonl"
    calls: list[str] = []
    durable_counts: list[int] = []
    monkeypatch.setattr(
        runner.os,
        "fsync",
        lambda _fd: durable_counts.append(len(out.read_text().splitlines())),
    )

    def invoke(
        task: ModelTaskBundle, _spec: runner.BackendSpec, **_kwargs: Any
    ) -> runner.Invocation:
        calls.append(task.task_id)
        if task.task_id == "retry" and len(calls) == 3:
            raise RuntimeError("missing harness receipt")
        return runner.parse_deployed_receipt(receipt())

    monkeypatch.setattr(runner, "invoke_pinned", invoke)
    argv = [
        "run_ladder.py",
        "--backend",
        "local-coder",
        "--set",
        "tiered",
        "--classes",
        "reasoning",
        "--difficulty",
        "easy",
        "--out",
        str(out),
    ]
    monkeypatch.setattr(runner.sys, "argv", argv)
    assert runner.main() == 0
    assert calls == ["0", "1", "retry"]
    assert durable_counts == [1, 2, 3]
    written = tables.load_jsonl([out])
    assert len(written) == 3
    retry = next(r for r in written if r["task_id"] == "retry")
    assert retry["score"]["outcome"] == "harness_error"
    assert "harness_error" in retry["notes"]
    transcript = json.loads(
        (out.parent / "responses/tiered-local-coder/0.json").read_text()
    )
    assert set(transcript) == {"task_id", "path", "sha256", "response"}
    assert transcript["response"] == "held value"
    assert not runner.RESULTS.exists()
    before = out.read_bytes()
    assert runner.main() == 2
    assert str(out) in capsys.readouterr().err
    assert out.read_bytes() == before
    monkeypatch.setattr(runner.sys, "argv", [*argv, "--resume"])
    assert runner.main() == 0
    assert calls == ["0", "1", "retry", "retry"]
    assert len(out.read_text().splitlines()) == 4
    assert durable_counts == [1, 2, 3, 4]
    assert all(r["accepted"] for r in tables.load_jsonl([out]))
    assert runner.main() == 0
    assert len(calls) == 4


def test_old_runner_cli_keeps_single_json_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    onex = tmp_path / "omnibase_infra/scripts/onex"
    onex.parent.mkdir(parents=True)
    onex.touch()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "HERE", tmp_path)
    monkeypatch.setattr(runner, "RESULTS", tmp_path / "results")
    source = tmp_path / "bundles"
    source.mkdir()
    (source / "task.json").write_text(bundle().model_dump_json())
    monkeypatch.setattr(runner, "BUNDLES", source)
    monkeypatch.setattr(
        runner, "invoke_local", lambda *_args: runner.parse_deployed_receipt(receipt())
    )
    out = tmp_path / "old.json"
    monkeypatch.setattr(
        runner.sys, "argv", ["run_ladder.py", "--path", "local", "--out", str(out)]
    )
    assert runner.main() == 0
    report = json.loads(out.read_text())
    assert set(report) == {
        "path",
        "generated_at",
        "concurrency",
        "base_url",
        "transcripts",
        "paid_escalation_disabled",
        "records",
    }
    assert report["path"] == "local"
    assert len(report["records"]) == 1
    assert report["records"][0]["backend"] is None
    assert not list(runner.RESULTS.glob("*.jsonl"))


def test_jsonl_cli_prints_tables_and_exports_same_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = records("a", 1, 2) + records("b", 1, 2)
    raw[0]["cost_usd"] = 0.0
    raw.append(
        {
            **records("unpinned", 1, 1)[0],
            "attempts": runner.parse_deployed_receipt(unpinned_receipt()).attempts,
            "escalation_count": 1,
        }
    )
    path, out = tmp_path / "input.jsonl", tmp_path / "report.json"
    path.write_text("".join(json.dumps(r) + "\n" for r in raw))
    argv = ["summarise_results.py", "--jsonl", str(path), "--out", str(out)]
    monkeypatch.setattr(tables.sys, "argv", argv)
    assert tables.main() == 0
    printed = capsys.readouterr().out
    assert "| class | backend | n | accepted | share | 95% CI |" in printed
    assert "| class | difficulty | backend |" in printed
    assert "50.0%" in printed
    assert "$0.0000 (1/2 receipts)" in printed
    # The chain is not a rung: it appears only in its own escalation table.
    assert "| unpinned |" not in printed.split("| task | answering backend |")[0]
    assert "First failing tier: reasoning / a: easy" in printed
    assert "reasoning: none separated" in printed
    assert "| task | answering backend |" in printed
    assert "first:refuse:below gate" in printed
    exported = json.loads(out.read_text())
    assert exported["class_rows"] == tables.class_rows(
        [r for r in raw if r["backend"] != "unpinned"]
    )
    assert exported["tier_rows"] == tables.tier_rows(
        [r for r in raw if r["backend"] != "unpinned"]
    )
    assert exported["escalation_rows"] == tables.escalation_rows(raw)
    assert exported["first_failing_tier"][0]["difficulty"] == "easy"
    monkeypatch.setattr(tables.sys, "argv", [*argv, str(path)])
    with pytest.raises(SystemExit) as exc:
        tables.main()
    assert exc.value.code == 2


def test_touching_intervals_are_not_separated() -> None:
    rows = [
        {"task_type": "review", "backend": "a", "lo": 0.0, "hi": 0.5},
        {"task_type": "review", "backend": "b", "lo": 0.5, "hi": 1.0},
    ]
    assert tables.separation(rows)["review"]["separated"] == []
    rows[1]["lo"] = 0.6
    assert tables.separation(rows)["review"]["separated"] == ["a", "b"]


def _reached(**over: Any) -> dict[str, Any]:
    base = records("a", 1, 1)[0]
    base.update({"attempts": [{"backend_id": "a"}], "response_chars": 5, "notes": ""})
    base.update(over)
    return base


def test_a_cell_that_never_reached_a_model_is_not_a_wrong_answer() -> None:
    timeout = _reached(
        attempts=[],
        response_chars=0,
        notes="status=timeout; error_message=Timed out waiting for terminal",
        score={"outcome": "fail"},
    )
    assert tables.is_unreached(timeout)
    assert tables.is_unreached(_reached(score={"outcome": "harness_error"}))
    assert tables.is_unreached(_reached(score={"outcome": "refused"}))
    # Negative controls: a wrong answer, and a gate refusal, are measurements.
    assert not tables.is_unreached(_reached(score={"outcome": "fail"}))
    assert not tables.is_unreached(
        _reached(
            notes="status=completed; error_message=",
            attempts=[{"backend_id": "a"}],
            score={"outcome": "fail"},
        )
    )


def test_alias_folds_the_same_model_on_a_second_host_into_one_rung(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = records("a", 2, 2) + records("a2", 2, 2)
    for i, record in enumerate(raw):
        record["task_id"] = f"t{i}"
    path = tmp_path / "in.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in raw))
    argv = ["summarise_results.py", "--jsonl", str(path), "--alias", "a2=a"]
    monkeypatch.setattr(tables.sys, "argv", argv)
    assert tables.main() == 0
    printed = capsys.readouterr().out
    assert "| reasoning | a | 4 | 4 |" in printed
    assert "a2" not in printed


def test_in_process_overlay_moves_a_deployed_rung_off_the_shared_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["env"] = argv, kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(receipt()) + "\n", "")

    runner.invoke_pinned(
        bundle(),
        runner.BACKENDS["openrouter-nemotron-super"],
        onex=tmp_path / "onex",
        timeout_s=3,
        run=fake_run,
        in_process_overlay="/tmp/overlay.yaml",
    )
    argv = seen["argv"]
    assert argv[argv.index("--locus") + 1] == "in-process"
    assert "--lane" not in argv
    assert seen["env"]["BIFROST_OVERLAY_PATH"] == "/tmp/overlay.yaml"
    assert argv[argv.index("--backend-id") + 1] == "openrouter-nemotron-super"


def test_one_rung_is_not_separated_from_nothing() -> None:
    rows = tables.class_rows(records("only", 25))
    assert tables.separation(rows)["reasoning"]["separated"] == []
    assert tables.separation(rows)["reasoning"]["verdict"] == "none separated"
