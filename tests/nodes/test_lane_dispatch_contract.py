# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch refusals, reconstructed from the recorded sender failures."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.lane_dispatch.model_lane_dispatch import (
    ModelLaneDispatchPolicy,
    ModelLaneDispatchRecord,
    ModelLaneDispatchSpec,
)
from omnimarket.nodes.node_lane_dispatch_effect.handlers.handler_lane_dispatch import (
    HandlerLaneDispatch,
    render_brief,
    runner_arguments,
    supervisor_spec,
)
from omnimarket.nodes.node_lane_dispatch_effect.models.model_lane_dispatch_effect import (
    ModelLaneDispatchOverlay,
    ModelLaneDispatchRequest,
)

CONTRACT = Path("src/omnimarket/nodes/node_lane_dispatch_effect/contract.yaml")


def policy() -> dict[str, Any]:
    data = yaml.safe_load(CONTRACT.read_text())["dispatch_policy"]
    data["hosts"] = [{"name": name, "can_handoff": False} for name in ("h051", "h053")]
    data["repository_names"] = ["widgets", "example/other"]
    data["launching_actors"] = ["launching-runner"]
    return data


def spec_data() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "job_id": "lj-0123456789abcdef",
        "repo": "example/widgets",
        "ref": "a" * 40,
        "pr": ["widgets#42"],
        "ticket": "OMN-19998",
        "parent": "orchestrator-31d9",
        "lane": "dispatch-proof",
        "lane_kind": "small-code",
        "placement": "lab",
        "host": "h053",
        "model": "codex",
        "effort": "high",
        "priority_class": "other",
        "title": "Use one checked dispatch record",
        "premises": [
            {
                "claim": "The parser accepts the old input",
                "evidence": "pytest parser => pass",
            }
        ],
        "task": "Update the parser in this repository and test it.",
        "memory_limit_bytes": 8589934592,
        "publish_steps": ["stage", "commit", "push", "pr", "handoff"],
        "handoff": {"actor": "launching-runner", "mode": "launcher"},
        "dependencies": [],
        "supersede": [],
        "ticket_state_ack": None,
        "time_box_min": 90,
        "retry_policy": {
            "max_attempts": 2,
            "backoff_s": 300,
            "on_time_box": "continue",
        },
        "done_criteria": [{"kind": "terminal_row"}],
        "rules": "Standing rules copied by the brief reader.",
        "constraints": (
            "## 3a. Constraints (pasted, never paraphrased)\n\n"
            "### 3a.5 Model and effort\n\nmodel=codex effort=high\n\n"
            "### 3a.8 CLAIM template\n\n"
            "stamp | CLAIM | lane=dispatch-proof | ticket=OMN-19998 | "
            "parent=orchestrator-31d9 | repo=widgets | actor=codex | scope=parser\n"
        ),
        "report": "Return the result to the parent.",
        "facts": {
            "source": "watcher and ledger readback",
            "read_at": "2026-11-04T16:00:00Z",
            "ticket_state": "In Progress",
            "merged_work": False,
            "umbrella": False,
            "live_claims": [],
        },
        "policy": policy(),
    }


def build(**changes: Any) -> ModelLaneDispatchSpec:
    return ModelLaneDispatchSpec.model_validate({**spec_data(), **changes})


def test_canonical_record_round_trip_and_tamper_refusal() -> None:
    spec = build()
    record = ModelLaneDispatchRecord.from_spec(spec)
    assert (
        ModelLaneDispatchRecord.model_validate_json(record.canonical_json()) == record
    )
    reordered = dict(reversed(list(json.loads(record.canonical_json()).items())))
    assert (
        ModelLaneDispatchRecord.model_validate(reordered).canonical_json()
        == record.canonical_json()
    )
    reordered["spec"]["host"] = "h051"
    with pytest.raises(ValidationError, match="DISPATCH-HASH-MISMATCH"):
        ModelLaneDispatchRecord.model_validate(reordered)


def test_published_schema_and_serialized_example_round_trip() -> None:
    record_text = Path("tests/fixtures/lane_dispatch/record.json").read_text()
    record = ModelLaneDispatchRecord.model_validate_json(record_text)
    assert record.canonical_json() + "\n" == record_text
    schema_path = CONTRACT.parent / "contracts/lane_dispatch.schema.json"
    assert (
        json.loads(schema_path.read_text())
        == ModelLaneDispatchRecord.model_json_schema()
    )


@pytest.mark.parametrize("prefix", ["https://", "http://", "//"])
def test_repository_urls_use_the_same_scope_rule(prefix: str) -> None:
    with pytest.raises(ValidationError, match="REMOTE-LANE-DEPENDENCY-REFUSED"):
        build(task=f"Read {prefix}forge.invalid/example/unlisted first.")
    assert build(task=f"Read {prefix}forge.invalid/example/widgets first.")


@pytest.mark.parametrize("field", ["unknown", "model_override"])
def test_extra_fields_and_mutation_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        build(**{field: "unexpected"})
    with pytest.raises(ValidationError, match="frozen"):
        build().host = "h051"


@pytest.mark.parametrize(
    ("patch", "code", "recorded"),
    [
        (
            {"placement": "cloud", "lane_kind": "ticket"},
            "REMOTE-LANE-CLOUD-REFUSED",
            "lane kind ticket is not repo-only",
        ),
        (
            {"placement": "cloud", "model": "codex"},
            "REMOTE-LANE-CLOUD-REFUSED",
            "a non-Claude model with cloud placement",
        ),
        (
            {"facts.umbrella": True},
            "REMOTE-LANE-UMBRELLA-REFUSED",
            "REMOTE-LANE-UMBRELLA-REFUSED",
        ),
        (
            {"repo": ""},
            "REMOTE-LANE-JOB-SPEC-REFUSED",
            "sweep's diagnose lane: missing repo or parent",
        ),
        (
            {"parent": ""},
            "REMOTE-LANE-JOB-SPEC-REFUSED",
            "18 fill lanes: missing repo or parent",
        ),
        (
            {
                "facts.live_claims": [
                    {"lane": "pr-5643-1503", "stamp": "2026-11-04T15:06:40Z"}
                ]
            },
            "REMOTE-LANE-LIVE-CLAIM-REFUSED",
            "ticket held by a live lane pr-5643-1503 without a supersede",
        ),
        (
            {"facts.merged_work": True},
            "REMOTE-LANE-TICKET-STATE-REFUSED",
            "merged work and no ticket-state-ack",
        ),
        (
            {
                "dependencies": [
                    {
                        "repo": "example/other",
                        "path": "src/input.py",
                        "pr": "other#7",
                        "merged": False,
                    }
                ]
            },
            "REMOTE-LANE-DEPENDENCY-REFUSED",
            "file in an unmerged PR of another repository",
        ),
        (
            {"task": "Read example/other/src/input.py first."},
            "REMOTE-LANE-DEPENDENCY-REFUSED",
            "another repository at all: 27 lanes stopped",
        ),
        (
            {"task": "Run git fetch origin dev before editing."},
            "LANE-BRIEF-REFUSED",
            "git fetch",
        ),
        (
            {"handoff": {"actor": "dispatch-proof", "mode": "lane"}},
            "REMOTE-LANE-HANDOFF-REFUSED",
            "PR hand-off expected from a lab host that cannot make it",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_recorded_failure_is_a_typed_build_error(
    patch: dict[str, Any], code: str, recorded: str
) -> None:
    data = spec_data()
    for path, value in patch.items():
        if path.startswith("facts."):
            data["facts"][path.removeprefix("facts.")] = value
        else:
            data[path] = value
    with pytest.raises(ValidationError) as caught:
        ModelLaneDispatchSpec.model_validate(data)
    assert caught.value.errors()[0]["type"] == code, recorded


def test_explicit_supersede_ack_and_cloud_claude_are_accepted() -> None:
    data = spec_data()
    data["facts"]["live_claims"] = [
        {"lane": "pr-5643-1503", "stamp": "2026-11-04T15:06:40Z"}
    ]
    data["facts"]["merged_work"] = True
    data["supersede"] = copy.deepcopy(data["facts"]["live_claims"])
    data["ticket_state_ack"] = "OMN-19998:merged"
    data.update(placement="cloud", model="sonnet")
    data["constraints"] = data["constraints"].replace("model=codex", "model=sonnet")
    assert ModelLaneDispatchSpec.model_validate(data).host == "h053"


def test_policy_is_data_and_changes_the_result_without_a_code_change() -> None:
    data = spec_data()
    changed = copy.deepcopy(policy())
    changed["cloud_lane_kinds"].append("ticket")
    changed["cloud_models"].append("codex")
    data.update(placement="cloud", lane_kind="ticket", policy=changed)
    assert ModelLaneDispatchSpec.model_validate(data).placement == "cloud"
    assert ModelLaneDispatchPolicy.model_validate(changed) != build().policy


class RecordingCommand:
    def __init__(self, checker_exit: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.checker_exit = checker_exit

    def run(self, argv: Sequence[str], timeout_s: int) -> tuple[int, str]:
        assert timeout_s == 30
        self.calls.append(tuple(argv))
        return (
            (self.checker_exit, "checker readback")
            if len(self.calls) == 1
            else (0, "runner readback")
        )


def effect_overlay(**changes: Any) -> ModelLaneDispatchOverlay:
    return ModelLaneDispatchOverlay.model_validate(
        {
            "policy": policy(),
            "checker_command": ["existing-brief-checker"],
            "runner_command": ["existing-remote-runner"],
            "command_timeout_s": 30,
            "event_bus": "inmemory",
            "kafka_bootstrap": None,
            "bus_lane": None,
            **changes,
        }
    )


def effect_request(
    tmp_path: Path, spec: ModelLaneDispatchSpec | None = None, *, execute: bool = False
) -> ModelLaneDispatchRequest:
    return ModelLaneDispatchRequest(
        record_json=ModelLaneDispatchRecord.from_spec(spec or build()).canonical_json(),
        output_dir=str(tmp_path),
        execute=execute,
    )


def test_production_handler_forwards_every_runner_field_and_keeps_h053(
    tmp_path: Path,
) -> None:
    command = RecordingCommand()
    spec = build(
        done_criteria=[
            {
                "kind": "check_passing",
                "target": "example/widgets#42",
                "check_context": "unit",
            }
        ]
    )
    result = HandlerLaneDispatch(
        effect_overlay(), command, lambda: datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    ).handle(effect_request(tmp_path, spec, execute=True))
    assert result.status == "submitted"
    argv = result.runner_argv
    expected = {
        "--owner": "example",
        "--repo": "widgets",
        "--sha": spec.ref,
        "--lane": spec.lane,
        "--ticket": spec.ticket,
        "--parent": spec.parent,
        "--placement": spec.placement,
        "--host": "h053",
        "--model": spec.model,
        "--effort": spec.effort,
        "--priority-class": spec.priority_class,
        "--mem-max": str(spec.memory_limit_bytes),
        "--timeout-min": str(spec.time_box_min),
        "--job-id": spec.job_id,
        "--max-attempts": str(spec.retry_policy.max_attempts),
        "--on-time-box": spec.retry_policy.on_time_box.value,
        "--pr": "widgets#42",
        "--done": "check_passing:widgets#42:unit",
        "--brief": result.brief_path,
    }
    assert {flag: argv[argv.index(flag) + 1] for flag in expected} == expected
    assert command.calls[-1] == argv
    # The runner's brief points at the exact spec, including fields with no legacy flag.
    persisted = ModelLaneDispatchRecord.model_validate_json(
        Path(result.record_path).read_text()
    )
    assert persisted.spec == spec
    assert (
        f"Dispatch record: {result.record_path}" in Path(result.brief_path).read_text()
    )
    for text in (
        spec.title,
        spec.task,
        spec.lane_kind,
        spec.premises[0].claim,
        spec.premises[0].evidence,
        spec.handoff.actor,
        ", ".join(spec.publish_steps),
    ):
        assert text in result.brief
    projected = supervisor_spec(spec, result.brief)
    assert projected.ref == spec.ref
    assert projected.host_preference == ("h053",)
    assert projected.retry_policy == spec.retry_policy
    assert projected.done_criteria == spec.done_criteria


def test_checker_refusal_returns_no_runner_call_or_argv(tmp_path: Path) -> None:
    command = RecordingCommand(checker_exit=3)
    result = HandlerLaneDispatch(
        effect_overlay(), command, lambda: datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    ).handle(effect_request(tmp_path, execute=True))
    assert result.error_code == "LANE-BRIEF-REFUSED"
    assert result.runner_argv == ()
    assert len(command.calls) == 1


def test_plain_serialized_spec_becomes_the_canonical_job_record(tmp_path: Path) -> None:
    data = spec_data()
    del data["policy"]
    request = ModelLaneDispatchRequest(
        record_json=json.dumps(data), output_dir=str(tmp_path)
    )
    command = RecordingCommand()
    result = HandlerLaneDispatch(
        effect_overlay(), command, lambda: datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    ).handle(request)
    assert result.status == "checked"
    record = ModelLaneDispatchRecord.model_validate_json(
        Path(result.record_path).read_text()
    )
    assert record.spec == build()
    assert record.content_hash == result.content_hash


def test_incomplete_envelope_is_never_rehashed_or_executed(tmp_path: Path) -> None:
    request = ModelLaneDispatchRequest(
        record_json=json.dumps({"spec": spec_data()}),
        output_dir=str(tmp_path),
        execute=True,
    )
    command = RecordingCommand()
    result = HandlerLaneDispatch(effect_overlay(), command).handle(request)
    assert result.status == "refused"
    assert command.calls == []


@pytest.mark.parametrize("case", ["stale", "future", "policy"])
def test_stale_facts_or_changed_policy_never_reaches_a_command(
    tmp_path: Path, case: str
) -> None:
    overlay = effect_overlay()
    moment = datetime(2026, 11, 4, 17 if case == "stale" else 15, tzinfo=UTC)
    if case == "policy":
        data = overlay.model_dump(mode="json")
        data["policy"]["max_memory_bytes"] += 1
        overlay = ModelLaneDispatchOverlay.model_validate(data)
        moment = datetime(2026, 11, 4, 16, 1, tzinfo=UTC)
    command = RecordingCommand()
    result = HandlerLaneDispatch(overlay, command, lambda: moment).handle(
        effect_request(tmp_path)
    )
    assert result.status == "refused"
    assert not command.calls
    assert not list(tmp_path.iterdir())


def test_supersession_reaches_the_unchanged_checkers_claim_section(
    tmp_path: Path,
) -> None:
    claim = {"lane": "pr-5643-1503", "stamp": "2026-11-04T15:06:40Z"}
    facts = spec_data()["facts"]
    facts["live_claims"] = [claim]
    spec = build(facts=facts, supersede=[claim])
    brief = render_brief(
        ModelLaneDispatchRecord.from_spec(spec), tmp_path / "record.json"
    )
    claim_section = brief.split("### 3a.8", 1)[1].split("## 4.", 1)[0]
    assert "supersedes-claim=2026-11-04T15:06:40Z" in claim_section
    claim = {**claim, "stamp": "2026-11-04T15:07:40Z"}
    with pytest.raises(ValidationError, match="REMOTE-LANE-LIVE-CLAIM-REFUSED"):
        build(facts=facts, supersede=[claim])


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("model=codex", "model=sonnet"),
        ("repo=widgets", "repo=other"),
        ("lane=dispatch-proof", "lane=other"),
        ("ticket=OMN-19998", "ticket=OMN-1"),
        ("parent=orchestrator-31d9", "parent=other"),
    ],
)
def test_stale_constraints_are_a_typed_build_error(before: str, after: str) -> None:
    with pytest.raises(ValidationError, match="DISPATCH-IDENTITY-REFUSED"):
        build(constraints=spec_data()["constraints"].replace(before, after))


@pytest.mark.parametrize(
    "patch",
    [
        {"task": "Required host: h051"},
        {"done_criteria": [{"kind": "terminal_row", "target": "example/widgets#42"}]},
        {"done_criteria": [{"kind": "pr_merged", "target": "other/widgets#42"}]},
        {"done_criteria": [{"kind": "exit_zero", "check_context": "unit"}]},
        {"risk_class": "doc"},
        {"writable": ["src/*"]},
        {"retry_policy": {"backoff_s": 1}},
    ],
)
def test_unrepresentable_legacy_arguments_are_refused(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build(**patch)


@pytest.mark.parametrize(
    ("model", "options"),
    [
        ("glm", {"risk_class": "doc"}),
        (
            "code-edit",
            {
                "writable": ["src/*"],
                "checks": ["unit=pytest test_parser.py"],
                "context": ["src/parser.py"],
                "max_turns": 4,
            },
        ),
        ("opus", {"escalation_reason": "complexity:B4"}),
    ],
)
def test_engine_specific_fields_reach_runner(
    model: str, options: dict[str, Any]
) -> None:
    spec = build(
        model=model,
        constraints=spec_data()["constraints"].replace("model=codex", f"model={model}"),
        **options,
    )
    argv = runner_arguments(spec, Path("brief.md"))
    flags = {
        "risk_class": "--risk-class",
        "writable": "--writable",
        "checks": "--check",
        "context": "--context",
        "max_turns": "--max-turns",
        "escalation_reason": "--escalation-reason",
    }
    for field, value in options.items():
        assert argv[argv.index(flags[field]) + 1] == str(
            value[0] if isinstance(value, list) else value
        )
    assert ("--effort" in argv) == (model in spec.policy.runner_effort_models)


def configure_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bus: str = "inmemory",
    broker: str | None = None,
    bus_lane: str | None = None,
) -> ModelLaneDispatchRequest:
    checker = tmp_path / "checker.py"
    checker.write_text(
        "import pathlib,sys\nassert sys.argv[1] == 'check'\nassert pathlib.Path(sys.argv[2]).is_file()\nassert sys.argv[3:] == ['--lane', 'dispatch-proof']\n"
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json,pathlib,sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
    )
    overlay = effect_overlay(
        checker_command=[sys.executable, str(checker)],
        runner_command=[
            sys.executable,
            str(runner),
            str(tmp_path / "runner-observed.json"),
        ],
        event_bus=bus,
        kafka_bootstrap=broker,
        bus_lane=bus_lane,
    )
    root = tmp_path / "overlays"
    target = root / "lane_dispatch"
    target.mkdir(parents=True)
    (target / "overlay.yaml").write_text(
        yaml.safe_dump(overlay.model_dump(mode="json"))
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    facts = spec_data()["facts"]
    facts["read_at"] = datetime.now(UTC).isoformat()
    return effect_request(tmp_path / "artifacts", build(facts=facts), execute=True)
