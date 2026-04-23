# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the Track B POC handler (ADK eval spike, P7)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from omnibase_core.enums.enum_lint_severity import EnumLintSeverity
from omnibase_core.enums.enum_type_debt_priority import EnumTypeDebtPriority
from omnibase_core.models.quality.model_mypy_finding import ModelMypyFinding
from omnibase_core.models.quality.model_type_debt_report import ModelTypeDebtReport
from omnibase_infra.adapters.llm.model_llm_adapter_response import (
    ModelLlmAdapterResponse,
)

from omnimarket.experiments.adk_eval.type_debt_scout_poc.handler_type_debt_scout import (
    ModelTrackBConfig,
    _extract_json_object,
    _parse_priorities,
    run_type_debt_scout,
)


def _sample_findings() -> list[ModelMypyFinding]:
    return [
        ModelMypyFinding(
            file="src/module_a.py",
            line=10,
            column=4,
            severity=EnumLintSeverity.ERROR,
            error_code="no-any-return",
            message="Returning Any from function declared to return 'T'",
        ),
        ModelMypyFinding(
            file="src/module_a.py",
            line=42,
            column=None,
            severity=EnumLintSeverity.ERROR,
            error_code="unused-ignore",
            message="Unused 'type: ignore' comment",
        ),
    ]


def _fake_response_payload() -> dict[str, Any]:
    return {
        "findings_prioritized": [
            {
                "finding_ref": "src/module_a.py:10",
                "priority": "critical",
                "rationale": "Any leak on hot path; erodes type safety.",
                "fix_sketch": "Tighten generic bound on T.",
            },
            {
                "finding_ref": "src/module_a.py:42",
                "priority": "noise",
                "rationale": "Unused ignore is pure cleanup.",
                "fix_sketch": None,
            },
        ]
    }


@pytest.mark.unit
class TestExtractJsonObject:
    def test_bare_json_passthrough(self) -> None:
        payload = '{"findings_prioritized": []}'
        assert _extract_json_object(payload) == payload

    def test_strips_markdown_fence(self) -> None:
        text = '```json\n{"findings_prioritized": []}\n```'
        extracted = _extract_json_object(text)
        assert json.loads(extracted) == {"findings_prioritized": []}

    def test_extracts_from_prose(self) -> None:
        text = 'Here is the result:\n{"findings_prioritized": [{"x": 1}]}\nThanks!'
        extracted = _extract_json_object(text)
        assert json.loads(extracted) == {"findings_prioritized": [{"x": 1}]}

    def test_raises_when_no_object(self) -> None:
        with pytest.raises(ValueError, match="no balanced JSON object"):
            _extract_json_object("sorry, no json here")


@pytest.mark.unit
class TestParsePriorities:
    def test_parses_priorities(self) -> None:
        raw = json.dumps(_fake_response_payload())
        priorities = _parse_priorities(raw)
        assert len(priorities) == 2
        refs = {p.finding_ref: p for p in priorities}
        assert refs["src/module_a.py:10"].priority is EnumTypeDebtPriority.CRITICAL
        assert refs["src/module_a.py:42"].fix_sketch is None

    def test_rejects_missing_list(self) -> None:
        with pytest.raises(ValueError, match="findings_prioritized"):
            _parse_priorities('{"other": []}')

    def test_rejects_non_object_root(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            _parse_priorities('["not", "an", "object"]')

    def test_collapses_duplicate_refs_keeps_most_severe(self) -> None:
        payload = {
            "findings_prioritized": [
                {
                    "finding_ref": "src/mod.py:5",
                    "priority": "noise",
                    "rationale": "trivial",
                    "fix_sketch": None,
                },
                {
                    "finding_ref": "src/mod.py:5",
                    "priority": "critical",
                    "rationale": "blast radius",
                    "fix_sketch": None,
                },
                {
                    "finding_ref": "src/mod.py:6",
                    "priority": "minor",
                    "rationale": "small",
                    "fix_sketch": None,
                },
            ]
        }
        result = _parse_priorities(json.dumps(payload))
        assert len(result) == 2
        by_ref = {p.finding_ref: p.priority for p in result}
        assert by_ref["src/mod.py:5"] is EnumTypeDebtPriority.CRITICAL
        assert by_ref["src/mod.py:6"] is EnumTypeDebtPriority.MINOR


@pytest.mark.unit
class TestRunTypeDebtScout:
    async def test_builds_report_from_router(self) -> None:
        config = ModelTrackBConfig(repo_name="unit-repo")
        findings = _sample_findings()
        fake_response = ModelLlmAdapterResponse(
            generated_text=json.dumps(_fake_response_payload()),
            model_used=config.model_id,
            usage_statistics={"prompt_tokens": 120, "completion_tokens": 80},
            finish_reason="stop",
            response_metadata={},
        )
        fake_router = AsyncMock()
        fake_router.generate_typed = AsyncMock(return_value=fake_response)

        report = await run_type_debt_scout(
            findings,
            config=config,
            router=fake_router,
        )

        assert isinstance(report, ModelTypeDebtReport)
        assert report.tool == "omnimarket_node"
        assert report.repo == "unit-repo"
        assert report.findings_total == len(findings)
        assert report.llm_calls == 1
        assert report.estimated_cost_usd == 0.0
        assert len(report.findings_prioritized) == 2
        fake_router.generate_typed.assert_awaited_once()
        request = fake_router.generate_typed.await_args.args[0]
        assert request.model_name == config.model_id
        assert "src/module_a.py:10" in request.prompt

    async def test_propagates_parse_failure(self) -> None:
        config = ModelTrackBConfig(repo_name="unit-repo")
        fake_response = ModelLlmAdapterResponse(
            generated_text="no json at all",
            model_used=config.model_id,
            usage_statistics={},
            finish_reason="stop",
            response_metadata={},
        )
        fake_router = AsyncMock()
        fake_router.generate_typed = AsyncMock(return_value=fake_response)
        with pytest.raises(ValueError, match="JSON object"):
            await run_type_debt_scout(
                _sample_findings(),
                config=config,
                router=fake_router,
            )
