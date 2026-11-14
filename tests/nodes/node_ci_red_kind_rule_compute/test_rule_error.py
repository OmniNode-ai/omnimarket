# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24322: the rule pre-classifier's error chain."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ci_red_kind_rule_compute import __main__ as rule_main
from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.enum_ci_red_kind_rule import (
    EnumCiRedKindRuleRefusal,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_answer import (
    ModelCiRedKindRuleAnswer,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)

pytestmark = pytest.mark.unit

REQUEST_JSON = json.dumps({"check_name": "delegation-health-check"})

VALID_OVERLAY = (
    "schema_version: ci_red_kind_rules.v1\n"
    "rules:\n"
    "  - kind: delegation-health-gate\n"
    "    check_name_pattern: 'delegation[- ]health'\n"
    "    ignore_case: true\n"
)


def test_an_unbound_overlay_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    handler = HandlerCiRedKindRule()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="delegation-health-check")
    )
    assert answer.ok is False
    assert answer.refusal is EnumCiRedKindRuleRefusal.OVERLAY_UNBOUND
    assert answer.model_call_required is False
    assert answer.matched is False
    assert answer.kind is None
    assert answer.detail


@pytest.mark.parametrize(
    "invalid_overlay",
    [
        pytest.param("rules: [unclosed", id="not-valid-yaml"),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: k\n"
            "    check_name_pattern: p\n"
            "    ignore_case: true\n"
            "    unknown: 1\n",
            id="unknown-key-in-a-rule",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: k\n"
            "    check_name_pattern: p\n",
            id="rule-missing-ignore_case",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\nrules: []\n",
            id="empty-rules-list",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v2\n"
            "rules:\n"
            "  - kind: k\n"
            "    check_name_pattern: p\n"
            "    ignore_case: true\n",
            id="wrong-schema_version",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: k\n"
            "    check_name_pattern: '('\n"
            "    ignore_case: true\n",
            id="pattern-that-does-not-compile",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: k\n"
            "    ignore_case: true\n",
            id="rule-with-no-pattern",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: k\n"
            "    log_pattern: '('\n"
            "    ignore_case: true\n",
            id="log-pattern-that-does-not-compile",
        ),
        pytest.param(
            "schema_version: ci_red_kind_rules.v1\n"
            "rules:\n"
            "  - kind: ''\n"
            "    check_name_pattern: p\n"
            "    ignore_case: true\n",
            id="empty-kind",
        ),
    ],
)
def test_an_invalid_overlay_file_is_refused_as_overlay_invalid(
    invalid_overlay: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = tmp_path / "overlay.yaml"
    pinned.write_text(invalid_overlay, encoding="utf-8")
    monkeypatch.setenv("CI_RED_KIND_RULES_OVERLAY", str(pinned))
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = HandlerCiRedKindRule()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="delegation-health-check")
    )
    assert answer.ok is False
    assert answer.refusal is EnumCiRedKindRuleRefusal.OVERLAY_INVALID
    assert answer.model_call_required is False
    assert answer.matched is False
    assert answer.kind is None
    assert answer.detail


def test_a_request_with_an_empty_check_name_fails_validation() -> None:
    with pytest.raises(ValidationError):
        ModelCiRedKindRuleRequest(check_name="")


def test_an_answer_that_is_matched_and_still_requires_a_model_call_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCiRedKindRuleAnswer(
            ok=True,
            matched=True,
            kind="k",
            rule_index=0,
            model_call_required=True,
            refusal=None,
            detail="",
        )


def test_an_answer_that_is_unmatched_and_carries_a_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCiRedKindRuleAnswer(
            ok=True,
            matched=False,
            kind="k",
            rule_index=None,
            model_call_required=True,
            refusal=None,
            detail="",
        )


def test_a_refusal_answer_that_says_ok_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelCiRedKindRuleAnswer(
            ok=True,
            matched=False,
            kind=None,
            rule_index=None,
            model_call_required=True,
            refusal=EnumCiRedKindRuleRefusal.OVERLAY_INVALID,
            detail="d",
        )


def test_the_main_entrypoint_returns_three_on_a_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(REQUEST_JSON))
    code = rule_main.main()
    assert code == 3
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is False
    assert answer["refusal"] == "overlay_unbound"
    assert answer["model_call_required"] is False


def test_the_main_entrypoint_returns_zero_on_a_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay_dir = tmp_path / "node_ci_red_kind_rule_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(VALID_OVERLAY, encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(REQUEST_JSON))
    code = rule_main.main()
    assert code == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True
    assert answer["matched"] is True
    assert answer["kind"] == "delegation-health-gate"
    assert answer["model_call_required"] is False


def test_a_request_may_carry_a_log_excerpt_and_refuses_an_unknown_field() -> None:
    request = ModelCiRedKindRuleRequest(check_name="x", log_excerpt="y")
    assert request.log_excerpt == "y"
    assert ModelCiRedKindRuleRequest(check_name="x").log_excerpt == ""
    with pytest.raises(ValidationError):
        ModelCiRedKindRuleRequest.model_validate(
            {"check_name": "x", "log_excerpt": "y", "unknown": 1}
        )


def test_the_main_entrypoint_returns_zero_on_a_log_rule_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay_dir = tmp_path / "node_ci_red_kind_rule_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "schema_version: ci_red_kind_rules.v1\n"
        "rules:\n"
        "  - kind: runner-infra\n"
        "    log_pattern: '##\\[error\\]Unexpected HTTP response: 5\\d\\d'\n"
        "    ignore_case: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "check_name": "Tests",
                    "log_excerpt": "##[error]Unexpected HTTP response: 504",
                }
            )
        ),
    )
    code = rule_main.main()
    assert code == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["matched"] is True
    assert answer["kind"] == "runner-infra"
    assert answer["model_call_required"] is False
