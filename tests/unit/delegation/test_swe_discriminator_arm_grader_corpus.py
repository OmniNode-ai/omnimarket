# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline coverage of SWE arm execution, the hard floor, and corpus loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.delegation.swe_discriminator import arm_runner, grader, model_client
from omnimarket.delegation.swe_discriminator.corpus import load_corpus
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumArm,
    EnumRouting,
    ModelCall,
    ModelSweDiscriminatorRuntimeConfig,
    SweTask,
)

_ADD = "def add(a, b):\n    return a + b + OFFSET"
_DOUBLE = "def double(value):\n    return value * 2"
_PLAN = '[{"slice_id": "sum", "instruction": "Implement add", "produces": ["add"]}]'


@pytest.fixture
def task() -> SweTask:
    return SweTask(
        task_id="arithmetic",
        level=1,
        source_pr="#1",
        source_sha="deadbeef",
        task_text="Fix add and double to implement arithmetic.",
        context_code="def add(a, b): return 0\ndef double(value): return 0",
        grader_preamble="OFFSET = 1",
        held_back_asserts="assert add(2, 3) == 6\nassert double(4) == 8",
        required_defs=["add", "double"],
    )


@pytest.fixture(autouse=True)
def forbid_real_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> ModelCall:
        pytest.fail("unit tests must inject chat instead of calling a real model")

    monkeypatch.setattr(model_client, "chat", forbidden)
    monkeypatch.setattr(arm_runner, "chat", forbidden)


def _call(
    role: str,
    content: str,
    *,
    cost: float = 0.0,
    latency: int = 0,
    error: str = "",
    status: int = 200,
) -> ModelCall:
    return ModelCall(
        role=role,
        tier="fake",
        model_name="offline",
        endpoint_label="offline",
        prompt_chars=0,
        content=content,
        cost_usd=cost,
        latency_ms=latency,
        error=error,
        http_status=status,
    )


class _FakeChat:
    def __init__(self) -> None:
        self.responses: list[ModelCall] = []
        self.requests: list[
            tuple[EnumRouting, str, str, ModelSweDiscriminatorRuntimeConfig | None]
        ] = []

    def __call__(
        self,
        tier: EnumRouting,
        prompt: str,
        *,
        role: str,
        runtime_config: ModelSweDiscriminatorRuntimeConfig | None = None,
    ) -> ModelCall:
        self.requests.append((tier, prompt, role, runtime_config))
        assert self.responses, "unexpected extra model call"
        response = self.responses.pop(0)
        assert response.role == role
        return response


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> _FakeChat:
    fake = _FakeChat()
    monkeypatch.setattr(arm_runner, "chat", fake)
    return fake


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("no arrays", []),
        ("[]", ["[]"]),
        ("```json\n[1, [2, 3]]\n```", ["[1, [2, 3]]"]),
        ("[str] preamble [1] then [2, 3]", ["[str]", "[1]", "[2, 3]"]),
        ("][1] [unfinished", ["[1]"]),
        ("[unclosed [1]", []),
        ("[not valid JSON]", ["[not valid JSON]"]),
    ],
)
def test_candidate_arrays(text: str, expected: list[str]) -> None:
    assert arm_runner._candidate_arrays(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        (" \n ", ""),
        (_ADD, _ADD),
        (f"Prose\n```python\n{_ADD}\n```\nmore prose", _ADD),
        (f"```py\n{_ADD}\n```", _ADD),
        (f"```\n{_ADD}\n```", _ADD),
        (f"```python\n{_ADD}\n```\n```PYTHON\n{_DOUBLE}\n```", _DOUBLE),
        (f"<think>discard this code</think>\n{_ADD}", _ADD),
        ("<think>unfinished reasoning", ""),
        (f"```python\n{_ADD}", f"```python\n{_ADD}"),
        ("malformed model prose", "malformed model prose"),
    ],
)
def test_extract_code(text: str, expected: str) -> None:
    assert arm_runner._extract_code(text) == expected


@pytest.mark.parametrize(
    "content",
    [
        _PLAN,
        f"```json\n{_PLAN}\n```",
        f"Preamble [str] [OMN-example]\n{_PLAN}",
        f"{_PLAN}\n[malformed JSON]",
        f"{_PLAN}\n[]",
        f"{_PLAN}\n[unfinished",
        f"<think>{_PLAN}</think>",
        f"<think>{_PLAN}",
    ],
)
def test_parse_slices_recovers_plan(content: str) -> None:
    assert arm_runner._parse_slices(content) == [
        {"slice_id": "sum", "instruction": "Implement add", "produces": ["add"]}
    ]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "prose",
        "[]",
        "[broken]",
        '[{"instruction":',
        '[{}, null, 1, "x"]',
        '[{"instruction": ""}, {"instruction": null}]',
    ],
)
def test_parse_slices_rejects_unusable_plan(content: str) -> None:
    assert arm_runner._parse_slices(content) is None


def test_parse_slices_prefers_last_plan_and_filters_items() -> None:
    final = [{"instruction": "Implement double"}]
    mixed = [None, "bad row", {}, {"instruction": ""}, *final]
    assert arm_runner._parse_slices(f"{_PLAN}\n{json.dumps(mixed)}") == final


def test_parse_slices_ignores_plan_in_closed_reasoning() -> None:
    assert arm_runner._parse_slices(
        f'{_PLAN}<think>[{{"instruction": "wrong"}}]</think>'
    ) == json.loads(_PLAN)


@pytest.mark.parametrize("kind", ["monolith", "decompose", "slice"])
def test_prompts_include_task_source_and_names_without_held_back_asserts(
    task: SweTask, kind: str
) -> None:
    if kind == "monolith":
        prompt = arm_runner._monolith_prompt(task)
        assert "single fenced ```python block" in prompt
    elif kind == "decompose":
        prompt = arm_runner._decompose_prompt(task)
        assert "JSON array" in prompt
        for field in ("slice_id", "instruction", "produces"):
            assert field in prompt
        assert "1-4 objects" in prompt
    else:
        prompt = arm_runner._slice_prompt(
            task, "Implement arithmetic", task.required_defs
        )
        assert "SLICE INSTRUCTION:\nImplement arithmetic" in prompt
        assert "DEFINE THESE NAMES: ['add', 'double']" in prompt
    assert task.task_text in prompt
    assert task.context_code in prompt
    for name in task.required_defs:
        assert name in prompt
    assert task.held_back_asserts not in prompt
    assert task.grader_preamble not in prompt


@pytest.mark.parametrize(
    "arm", [EnumArm.A_MONOLITH_FRONTIER, EnumArm.B_MONOLITH_COST_ROUTED]
)
@pytest.mark.parametrize("error", ["", "bad response"])
def test_run_monolith_captures_output_and_error(
    task: SweTask, fake_chat: _FakeChat, arm: EnumArm, error: str
) -> None:
    config = ModelSweDiscriminatorRuntimeConfig()
    content = f"```python\n{_ADD}\n```"
    call = _call("monolith", content, cost=0.123456789, latency=17, error=error)
    fake_chat.responses = [call]
    run = arm_runner.run_arm(task, arm, config)
    assert fake_chat.requests == [
        (arm.routing, arm_runner._monolith_prompt(task), "monolith", config)
    ]
    assert not fake_chat.responses
    assert run.task_id == task.task_id
    assert run.arm == arm
    assert run.routing == arm.routing
    assert run.decomposition == arm.decomposition
    assert run.artifact == content
    assert run.calls == [call]
    assert run.n_slices == 1
    assert run.slice_plan == ["<whole-task monolith>"]
    assert run.error == (f"monolith worker error: {error}" if error else "")
    assert run.decomposition_tax_usd == 0
    assert run.total_cost_usd == 0.12345679
    assert run.total_latency_ms == 17
    assert not run.blocked


@pytest.mark.parametrize(
    "arm", [EnumArm.C_DECOMPOSED_FRONTIER, EnumArm.D_DECOMPOSED_COST_ROUTED]
)
@pytest.mark.parametrize("override_decomposer", [False, True])
def test_run_decomposed_routes_and_integrates_slices(
    task: SweTask, fake_chat: _FakeChat, arm: EnumArm, override_decomposer: bool
) -> None:
    config = (
        ModelSweDiscriminatorRuntimeConfig(decomposer_tier=EnumRouting.COST_ROUTED)
        if override_decomposer
        else None
    )
    plan = [
        {"slice_id": "sum", "instruction": "Implement add", "produces": ["add"]},
        {"instruction": "Implement double", "produces": ["double", 7]},
    ]
    calls = [
        _call("decomposer", json.dumps(plan), cost=0.100000004, latency=11),
        _call("worker", f"```python\n{_ADD}\n```", cost=0.2, latency=13),
        _call("worker", _DOUBLE, cost=0.3, latency=19),
    ]
    fake_chat.responses = calls.copy()
    run = arm_runner.run_arm(task, arm, config)
    dec_tier = EnumRouting.COST_ROUTED if override_decomposer else EnumRouting.FRONTIER
    assert fake_chat.requests == [
        (dec_tier, arm_runner._decompose_prompt(task), "decomposer", config),
        (
            arm.routing,
            arm_runner._slice_prompt(task, "Implement add", ["add"]),
            "worker",
            config,
        ),
        (
            arm.routing,
            arm_runner._slice_prompt(task, "Implement double", ["double", "7"]),
            "worker",
            config,
        ),
    ]
    assert not fake_chat.responses
    assert run.calls == calls
    assert run.artifact == f"{_ADD}\n\n{_DOUBLE}"
    assert run.slice_plan == ["sum", "slice_1"]
    assert run.n_slices == 2
    assert run.decomposition_tax_usd == calls[0].cost_usd
    assert run.total_cost_usd == 0.6
    assert run.total_latency_ms == 43
    assert run.error == ""
    assert not run.blocked


@pytest.mark.parametrize(
    ("content", "error"), [("not JSON", ""), ("[]", ""), (_PLAN, "bad response")]
)
def test_run_decomposed_falls_back_to_whole_task(
    task: SweTask, fake_chat: _FakeChat, content: str, error: str
) -> None:
    calls = [
        _call("decomposer", content, cost=0.04, latency=2, error=error),
        _call("worker", _ADD, cost=0.05, latency=3),
    ]
    fake_chat.responses = calls.copy()
    run = arm_runner.run_arm(task, EnumArm.D_DECOMPOSED_COST_ROUTED)
    assert fake_chat.requests[1] == (
        EnumRouting.COST_ROUTED,
        arm_runner._slice_prompt(task, task.task_text, task.required_defs),
        "worker",
        None,
    )
    assert not fake_chat.responses
    assert run.calls == calls
    assert run.slice_plan == ["<decomposition-degraded: whole task>"]
    assert run.n_slices == 1
    assert run.artifact == _ADD
    assert run.error == ""
    assert run.decomposition_tax_usd == 0.04
    assert run.total_cost_usd == 0.09
    assert run.total_latency_ms == 5


def test_run_decomposed_skips_failed_and_empty_workers(
    task: SweTask, fake_chat: _FakeChat
) -> None:
    plan = [
        {"instruction": "first", "produces": "not a list"},
        {"instruction": "second"},
        {"instruction": "third", "produces": []},
        {"instruction": "fourth", "produces": ["double"]},
    ]
    calls = [
        _call("decomposer", json.dumps(plan), cost=0.1, latency=1),
        _call("worker", _ADD, cost=0.2, latency=2, error="first failure"),
        _call("worker", _ADD, cost=0.3, latency=3, error="second failure"),
        _call("worker", "<think>unfinished", cost=0.4, latency=4),
        _call("worker", _DOUBLE, cost=0.5, latency=5),
    ]
    fake_chat.responses = calls.copy()
    run = arm_runner.run_arm(task, EnumArm.C_DECOMPOSED_FRONTIER)
    for request in fake_chat.requests[1:4]:
        assert "DEFINE THESE NAMES: []" in request[1]
    assert not fake_chat.responses
    assert run.calls == calls
    assert run.n_slices == 4
    assert run.artifact == _DOUBLE
    assert (
        run.error
        == "slice worker error: first failure; slice worker error: second failure"
    )
    assert run.total_cost_usd == 1.5
    assert run.total_latency_ms == 15
    assert not run.blocked


@pytest.mark.parametrize("arm", list(EnumArm))
def test_run_arm_marks_empty_infrastructure_failures_blocked(
    task: SweTask, fake_chat: _FakeChat, arm: EnumArm
) -> None:
    if arm in (EnumArm.A_MONOLITH_FRONTIER, EnumArm.B_MONOLITH_COST_ROUTED):
        calls = [
            _call("monolith", "", cost=0.1, latency=7, error="rate limited", status=429)
        ]
        expected_error = "monolith worker error: rate limited"
    else:
        calls = [
            _call(
                "decomposer", "", cost=0.04, latency=3, error="unreachable", status=0
            ),
            _call("worker", "", cost=0.06, latency=4, error="rate limited", status=429),
        ]
        expected_error = "slice worker error: rate limited"
    fake_chat.responses = calls.copy()
    run = arm_runner.run_arm(task, arm)
    assert not fake_chat.responses
    assert run.calls == calls
    assert run.artifact == ""
    assert run.error == expected_error
    assert run.blocked
    assert run.total_cost_usd == 0.1
    assert run.total_latency_ms == 7


@pytest.mark.parametrize(
    ("artifact", "error", "status", "blocked"),
    [
        ("", "rate limited", 429, True),
        (" \n", "unreachable", 0, True),
        ("", "unavailable", 503, True),
        ("", "bad gateway", 502, True),
        ("", "bad request", 400, False),
        ("", "", 429, False),
        (_ADD, "rate limited", 429, False),
    ],
)
def test_finalize_totals_and_infrastructure_block(
    artifact: str, error: str, status: int, blocked: bool
) -> None:
    arm = EnumArm.C_DECOMPOSED_FRONTIER
    run = ArmRun(
        task_id="arithmetic",
        arm=arm,
        decomposition=arm.decomposition,
        routing=arm.routing,
        artifact=artifact,
        calls=[
            _call("decomposer", "", cost=0.000000006, latency=7),
            _call("worker", "", cost=0.1, latency=8, error=error, status=status),
        ],
    )
    assert arm_runner._finalize(run) is run
    assert run.total_cost_usd == 0.10000001
    assert run.total_latency_ms == 15
    assert run.blocked is blocked


def test_finalize_empty_call_list() -> None:
    arm = EnumArm.A_MONOLITH_FRONTIER
    run = ArmRun(
        task_id="empty", arm=arm, decomposition=arm.decomposition, routing=arm.routing
    )
    assert arm_runner._finalize(run) is run
    assert run.total_cost_usd == 0
    assert run.total_latency_ms == 0
    assert not run.blocked


@pytest.mark.parametrize(
    ("code", "required", "expected"),
    [
        ("", [], []),
        ("", ["add", "Box"], ["add", "Box"]),
        ("async def add(a, b): pass\nclass Box: pass", ["add", "Box"], []),
        ("def addition(): pass\nclass Boxed: pass", ["add", "Box"], ["add", "Box"]),
        ("add = lambda: 1", ["add"], ["add"]),
        ("def add(): pass", ["double", "add", "Box"], ["double", "Box"]),
        ("def aXb(): pass", ["a.b"], ["a.b"]),
    ],
)
def test_missing_defs(code: str, required: list[str], expected: list[str]) -> None:
    assert grader.missing_defs(code, required) == expected


@pytest.mark.parametrize(
    ("artifact", "detail"),
    [
        ("", "no code recovered from artifact (empty/truncated)"),
        ("<think>unfinished", "no code recovered from artifact (empty/truncated)"),
        (_ADD, "artifact missing required defs: ['double']"),
    ],
)
def test_grade_floor_rejects_before_execution(
    task: SweTask, monkeypatch: pytest.MonkeyPatch, artifact: str, detail: str
) -> None:
    def forbidden(*args: object, **kwargs: object) -> tuple[bool, str]:
        pytest.fail("empty or incomplete artifacts must not execute")

    monkeypatch.setattr(grader, "run_code_asserts", forbidden)
    assert grader.grade_floor(task, artifact) == (False, detail)


@pytest.mark.parametrize("fenced", [False, True])
def test_grade_floor_passes_with_preamble_and_held_back_asserts(
    task: SweTask, fenced: bool
) -> None:
    code = f"{_ADD}\n\n{_DOUBLE}"
    artifact = f"```python\n{code}\n```" if fenced else code
    assert grader.grade_floor(task, artifact) == (True, "asserts passed")


@pytest.mark.parametrize(
    ("code", "failure"),
    [
        (f"def add(a, b): return 0\n{_DOUBLE}", "AssertionError"),
        (
            f"def add(a, b):\n    raise ValueError('bad arithmetic')\n{_DOUBLE}",
            "ValueError: bad arithmetic",
        ),
        (f"def add(a, b)\n    return a + b\n{_DOUBLE}", "SyntaxError"),
    ],
)
def test_grade_floor_fails_execution(task: SweTask, code: str, failure: str) -> None:
    passed, detail = grader.grade_floor(task, code)
    assert not passed
    assert detail.startswith("asserts failed:")
    assert failure in detail


def test_grade_floor_passes_executor_arguments(
    task: SweTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = f"{_ADD}\n{_DOUBLE}"

    def executor(
        candidate: str, *, entrypoint: str | None, asserts: str, timeout_s: float
    ) -> tuple[bool, str]:
        assert candidate == f"{task.grader_preamble}\n{code}"
        assert entrypoint is None
        assert asserts == task.held_back_asserts
        assert timeout_s == 20.0
        return False, "offline executor detail"

    monkeypatch.setattr(grader, "run_code_asserts", executor)
    assert grader.grade_floor(task, f"```python\n{code}\n```") == (
        False,
        "offline executor detail",
    )


def test_load_default_smoke_corpus() -> None:
    tasks = load_corpus()
    assert [task.task_id for task in tasks] == [
        "OMN-13964-create-ticket-field",
        "OMN-13816-state-coverage-ast",
    ]
    assert [task.level for task in tasks] == [1, 3]
    assert [task.required_defs for task in tasks] == [
        ["ModelCreateTicketRequest", "ModelCreateTicketResult"],
        ["_state_covered"],
    ]
    assert all(isinstance(task, SweTask) for task in tasks)
    assert all(task.context_code and task.held_back_asserts for task in tasks)


def test_load_custom_corpus(task: SweTask, tmp_path: Path) -> None:
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump({"tasks": [task.model_dump()]}))
    assert load_corpus(path) == [task]


def test_load_corpus_rejects_bad_row(task: SweTask, tmp_path: Path) -> None:
    path = tmp_path / "corpus.yaml"
    bad_row = task.model_dump()
    bad_row["level"] = 5
    path.write_text(yaml.safe_dump({"tasks": [task.model_dump(), bad_row]}))
    with pytest.raises(ValidationError) as exc:
        load_corpus(path)
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [
        (("level",), "less_than_equal")
    ]
