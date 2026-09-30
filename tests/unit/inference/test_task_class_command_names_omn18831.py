# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A command name or flag in a request is material, not the request (OMN-18831).

Run 33491e90-c87c-4fcf-8063-554616c984d7 (2026-09-29, FRICTION ledger
2026-09-29T22:49:35Z) asked for a five-line prose report. The report's facts
named the command ``generate-migration-inventory --write``, and
``code_generation``'s qualified phrase "generate" found the qualifier
"migration" inside the same command name. The request was classed as code,
and the compilation floor refused every rung: three local, two OpenRouter,
Gemini and GLM, for "response does not compile as Python". The answers were
correct prose.

The fix is syntactic and vocabulary-free, like the rest of
``request_instruction``: a flag (``--write``) and a kebab-case name of three
or more segments (``generate-migration-inventory``) name a command or an
option, so neither is read as request text. The quality gate is untouched:
a request that asks for code still reaches ``code_generation``.
"""

from __future__ import annotations

import pytest

from omnimarket.inference.request_instruction import instruction_text
from omnimarket.inference.task_class_authority import (
    EnumTaskTypeResolution,
    ModelTaskClassAuthority,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit

#: The prompt of run 33491e90, verbatim from its run.json.
_RUN_33491E90_PROMPT = (
    "Write a 5-line report for a red-PR fix lane. PR "
    "OmniNode-ai/onex_change_control#11837 (OMN-14888) was red on Migration "
    "Inventory Sync and CI Summary because migration_inventory.yaml lacked "
    "omnibase_infra migration "
    "109_delegation_workflow_state_updated_at_only_on_transition.sql. Fix: "
    "regenerated with generate-migration-inventory --write on lab host .202, "
    "one-line addition, check-migration-inventory and 23 focused tests passed, "
    "pushed 5235406a5 on top of 54a0dda42. Residual: OCC dev inventory is "
    "stale so other OCC PRs stay red until one lands the same line."
)


@pytest.fixture(name="production")
def _production() -> ModelTaskClassAuthority:
    return load_task_class_authority()


def _resolve(prompt: str, classes: ModelTaskClassAuthority) -> str:
    return classes.resolve_task_type(prompt, explicit=None).task_type


class TestTheRecordedPrompt:
    def test_the_prose_report_is_not_classed_as_code(
        self, production: ModelTaskClassAuthority
    ) -> None:
        resolved = _resolve(_RUN_33491E90_PROMPT, production)
        assert resolved != "code_generation"
        # Every class graded on compilation is equally wrong for a report.
        assert resolved not in {"test", "refactor", "code_generation"}

    def test_the_command_name_and_flag_are_not_request_words(self) -> None:
        text = instruction_text(_RUN_33491E90_PROMPT)
        assert "generate" not in text.split()
        assert "generate-migration-inventory" not in text
        assert "--write" not in text
        assert "check-migration-inventory" not in text

    def test_an_explicit_class_still_wins(
        self, production: ModelTaskClassAuthority
    ) -> None:
        resolution = production.resolve_task_type(
            _RUN_33491E90_PROMPT, explicit="document"
        )
        assert resolution.task_type == "document"
        assert resolution.resolution is EnumTaskTypeResolution.EXPLICIT


class TestCommandMaterial:
    @pytest.mark.parametrize(
        "token",
        [
            "--write",
            "-k",
            "--task-type=document",
            "generate-migration-inventory",
            "onex-lab-run",
            "check-migration-inventory,",
        ],
    )
    def test_a_flag_or_command_name_is_removed(self, token: str) -> None:
        assert token.rstrip(",") not in instruction_text(f"Run {token} now.")

    @pytest.mark.parametrize(
        "compound",
        ["write-up", "trade-offs", "one-line", "red-PR", "de-duplicate", "5-line"],
    )
    def test_a_two_segment_compound_is_kept(self, compound: str) -> None:
        assert compound in instruction_text(f"Give me the {compound} today.")

    @pytest.mark.parametrize("dash", ["a - b", "a -- b", "costs -5 dollars"])
    def test_a_dash_or_negative_number_is_not_an_option(self, dash: str) -> None:
        assert instruction_text(dash) == dash

    def test_a_code_request_that_names_a_command_is_still_code(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = (
            "Write a Python script that runs generate-migration-inventory "
            "--write and exits non-zero when the inventory changed."
        )
        assert _resolve(prompt, production) == "code_generation"

    def test_a_generate_request_with_a_code_artifact_is_still_code(
        self, production: ModelTaskClassAuthority
    ) -> None:
        prompt = "Generate a migration that adds an index on the runs table."
        assert _resolve(prompt, production) == "code_generation"
