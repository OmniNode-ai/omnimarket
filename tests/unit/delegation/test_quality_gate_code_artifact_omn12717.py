# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-12717: code-generation acceptance requires a real code artifact.

The lab corpus is the sanitized 65-item corpus captured by lane ``gate-f2-fix``
on 2026-09-25. Machine-local path prefixes were replaced with ``$HOME``,
``$OMNI_HOME``, and ``$VOLUME``; prompts, responses, labels, and oracle verdicts
are otherwise unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_CORPUS = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "delegation"
    / "omn12717_gate_corpus.json"
)
_CORRELATION_ID = UUID("12717000-0000-4000-8000-000000000001")
_FINISH_REASONS = (
    EnumProviderFinishReason.STOP,
    EnumProviderFinishReason.ABSENT,
)


class CorpusItem(TypedDict):
    id: str
    group: str
    task_type: str
    prompt: str
    response: str
    response_contract: dict[str, object] | None
    right: bool
    recorded_gate: bool | None


@dataclass(frozen=True)
class Verdict:
    item: CorpusItem
    finish_reason: EnumProviderFinishReason
    accepted: bool
    failure_reasons: tuple[str, ...]


def _items() -> list[CorpusItem]:
    return cast(list[CorpusItem], json.loads(_CORPUS.read_text(encoding="utf-8")))


def _verdict(
    item: CorpusItem,
    finish_reason: EnumProviderFinishReason,
    *,
    grounding_source: str | None = None,
) -> Verdict:
    deterministic, heuristic = resolve_task_class_dod_checks(
        item["task_type"], prompt=item["prompt"]
    )
    gate_input = ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type=item["task_type"],
        llm_response_content=item["response"],
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )
    result = delta(
        gate_input,
        response_contract=item["response_contract"],
        grounding_source=grounding_source,
        finish_reason=finish_reason,
    )
    return Verdict(
        item=item,
        finish_reason=finish_reason,
        accepted=result.passed,
        failure_reasons=result.failure_reasons,
    )


def test_fixture_is_the_complete_sanitized_lab_corpus() -> None:
    items = _items()
    assert len(items) == 65
    assert sum(item["group"] == "exercise_delivered" for item in items) == 16
    assert (
        sum(
            item["group"] == "oneword_to_task_prompt"
            and item["task_type"] == "code_generation"
            for item in items
        )
        == 32
    )
    serialized = _CORPUS.read_text(encoding="utf-8")
    assert "/Users/" not in serialized  # test-literal-ok: sanitization probe
    assert "/Volumes/" not in serialized  # test-literal-ok: sanitization probe


@pytest.mark.parametrize("finish_reason", _FINISH_REASONS, ids=lambda r: r.value)
def test_all_32_one_word_code_generation_replies_are_refused(
    finish_reason: EnumProviderFinishReason,
) -> None:
    candidates = [
        item
        for item in _items()
        if item["group"] == "oneword_to_task_prompt"
        and item["task_type"] == "code_generation"
    ]
    accepted = [
        verdict.item["id"]
        for item in candidates
        if (verdict := _verdict(item, finish_reason)).accepted
    ]
    assert accepted == []


@pytest.mark.parametrize("finish_reason", _FINISH_REASONS, ids=lambda r: r.value)
def test_every_oracle_correct_code_answer_passes(
    finish_reason: EnumProviderFinishReason,
) -> None:
    correct_code = [
        item
        for item in _items()
        if item["group"] == "exercise_delivered"
        and item["task_type"] == "code_generation"
        and item["right"]
    ]
    refused = [
        (verdict.item["id"], verdict.failure_reasons)
        for item in correct_code
        if not (verdict := _verdict(item, finish_reason)).accepted
    ]
    assert refused == []


@pytest.mark.parametrize("finish_reason", _FINISH_REASONS, ids=lambda r: r.value)
def test_the_16_exercise_answers_do_not_regress(
    finish_reason: EnumProviderFinishReason,
) -> None:
    """A correct answer never flips to refusal; a known refusal never opens."""
    baseline_acceptance = {
        "exercise:t01_dev_a3": True,
        "exercise:t02_dogfood_a3": True,
        "exercise:t03_dev_a3": True,
        "exercise:t04_dogfood_a1": True,
        "exercise:t04_dogfood_a3": True,
        "exercise:t05_dev_a1": False,
        "exercise:t05_dev_a3": True,
        "exercise:t05_dogfood_a2": False,
        "exercise:t06_dogfood_a1": False,
        "exercise:t06_dogfood_a3": True,
        "exercise:t07_dev_a1": True,
        "exercise:t07_dev_a3": True,
        "exercise:t07_dogfood_a2": True,
        "exercise:t08_dogfood_a3": True,
        "exercise:t09_dev_a3": True,
        "exercise:t10_dogfood_a3": True,
    }
    verdicts = {
        item["id"]: _verdict(item, finish_reason)
        for item in _items()
        if item["group"] == "exercise_delivered"
    }
    assert set(verdicts) == set(baseline_acceptance)

    regressions: list[str] = []
    for item_id, before_accepted in baseline_acceptance.items():
        verdict = verdicts[item_id]
        if verdict.item["right"] and before_accepted and not verdict.accepted:
            regressions.append(f"correct answer newly refused: {item_id}")
        if not verdict.item["right"] and not before_accepted and verdict.accepted:
            regressions.append(f"wrong answer newly accepted: {item_id}")
    assert regressions == []


def test_code_generation_contract_declares_code_artifact_evidence() -> None:
    deterministic, _ = resolve_task_class_dod_checks(
        "code_generation", prompt="Write a function named normalize_status."
    )
    assert "code_artifact_present" in deterministic


@pytest.mark.parametrize(
    "content",
    [
        "Done",
        "Yes",
        "Sure",
        "ok",
        '"finished"',
        "42",
        "None",
        "```python\nDone\n```",
    ],
)
def test_literal_or_single_name_python_is_not_a_code_artifact(content: str) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt="Define function normalize_status."
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="code_generation",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    assert not result.passed
    assert any("code artifact" in reason for reason in result.failure_reasons)


@pytest.mark.parametrize(
    "content",
    [
        "def normalize_status(value: str) -> str:\n    return value.strip().lower()",
        "class StatusNormalizer:\n    def normalize(self, value: str) -> str:\n"
        "        return value.strip().lower()",
        "```yaml\nstatus_map:\n  READY: ready\n```",
        '{"edits": [{"file": "module.py", "search": "old", '
        '"replace": "def normalize_status(value):\\n    return value"}]}',
    ],
)
def test_non_trivial_code_artifacts_pass_the_new_floor(content: str) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt="Define function normalize_status."
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="code_generation",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    assert result.passed, result.failure_reasons


def test_grounded_prompt_keeps_the_stronger_name_resolution_check() -> None:
    prompt = "Define function normalize_status without external dependencies."
    deterministic, heuristic = resolve_task_class_dod_checks(
        "code_generation", prompt=prompt
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="code_generation",
            llm_response_content=(
                "def normalize_status(value: str) -> str:\n"
                "    return missing_helper(value)"
            ),
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=prompt,
    )
    assert not result.passed
    assert "names_resolve" not in result.skipped_checks
    assert any("missing_helper" in reason for reason in result.failure_reasons)


def _test_artifact_verdict(content: str) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "test", prompt="Write runnable pytest unit tests for normalize_status."
    )
    return delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="test",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "def helper():\n    return True",
        "def helper():\n    def test_nested():\n        assert True",
        "class Helper:\n    def test_hidden(self):\n        assert True",
        "class TestHidden:\n    def __init__(self):\n        pass\n"
        "    def test_hidden(self):\n        assert True",
        'DESCRIPTION = "def test_in_a_string(): assert True"',
    ],
)
def test_test_generation_refuses_marked_modules_without_collectable_tests(
    body: str,
) -> None:
    result = _test_artifact_verdict(
        "import pytest\npytestmark = pytest.mark.unit\n" + body
    )
    assert not result.passed
    assert any("pytest_tests_present" in reason for reason in result.failure_reasons)
    assert result.fail_category == "fail_deterministic"


@pytest.mark.parametrize(
    "body",
    [
        "def test_normalize():\n    assert ' READY '.strip().lower() == 'ready'",
        "class TestNormalize:\n    def test_ready(self):\n"
        "        assert ' READY '.strip().lower() == 'ready'",
        "@pytest.mark.unit\ndef test_ready():\n    assert True",
    ],
)
@pytest.mark.parametrize("fenced", [False, True])
def test_test_generation_accepts_collectable_unit_tests(
    body: str, fenced: bool
) -> None:
    content = "import pytest\npytestmark = pytest.mark.unit\n" + body
    if fenced:
        content = "```python\n" + content + "\n```"
    result = _test_artifact_verdict(content)
    assert result.passed, result.failure_reasons


def test_test_contract_requires_pytest_test_artifact() -> None:
    deterministic, _ = resolve_task_class_dod_checks(
        "test", prompt="Write runnable pytest unit tests."
    )
    assert "pytest_tests_present" in deterministic


@pytest.mark.parametrize("task_type", ["test", "code_generation"])
def test_empty_final_content_is_refused_for_artifact_routes(task_type: str) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type=task_type,
            llm_response_content="",
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    assert not result.passed


@pytest.mark.parametrize("task_type", ["test", "code_generation"])
def test_length_vetoes_even_syntactically_complete_artifacts(task_type: str) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    result = delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type=task_type,
            llm_response_content=(
                "import pytest\npytestmark = pytest.mark.unit\n"
                "def test_ready():\n    assert ' READY '.strip().lower() == 'ready'"
            ),
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        finish_reason=EnumProviderFinishReason.LENGTH,
    )
    assert not result.passed
    assert any("finish_reason=length" in reason for reason in result.failure_reasons)
