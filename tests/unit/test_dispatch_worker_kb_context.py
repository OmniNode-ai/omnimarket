# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OMN-11939: knowledge_context_level enrichment in node_dispatch_worker.

TDD-first: these tests were written before the implementation.

Acceptance criteria tested:
- knowledge_context_level field exists with "none" default (backward compat)
- Prompt includes ## Knowledge Context section when level != "none"
- Result includes KB evidence fields: knowledge_context_bundle_hash,
  bundle_level, source_backends_used, degraded_backends, injected_context_char_count
- context_hash is referenced in dispatch evidence (no-hidden-context invariant)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)


def _make_fixer_cmd(**overrides: object) -> ModelDispatchWorkerCommand:
    defaults: dict[str, object] = {
        "name": "kb-test-fixer",
        "team": "test-team",
        "role": EnumWorkerRole.fixer,
        "scope": "Fix the thing",
        "targets": ["OMN-11939", "omnimarket#1"],
    }
    return ModelDispatchWorkerCommand(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.mark.unit
def test_knowledge_context_level_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compat: existing dispatches with no knowledge_context_level use 'none'."""
    cmd = _make_fixer_cmd()
    assert cmd.knowledge_context_level == "none"


@pytest.mark.unit
def test_knowledge_context_level_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All defined levels are accepted: none, L0, L1, L2, L3."""
    for level in ("none", "L0", "L1", "L2", "L3"):
        cmd = _make_fixer_cmd(knowledge_context_level=level)
        assert cmd.knowledge_context_level == level


@pytest.mark.unit
def test_knowledge_context_level_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid level values are rejected by Pydantic validation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_fixer_cmd(knowledge_context_level="L4")

    with pytest.raises(ValidationError):
        _make_fixer_cmd(knowledge_context_level="high")


@pytest.mark.unit
def test_prompt_has_no_kb_section_when_level_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default 'none' level: no Knowledge Context section injected into prompt."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="none")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert "## Knowledge Context" not in result.validated_prompt_template


@pytest.mark.unit
def test_prompt_has_kb_section_when_level_is_l1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Level L1: ## Knowledge Context section is injected into prompt."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L1")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert "## Knowledge Context" in result.validated_prompt_template


@pytest.mark.unit
def test_prompt_has_kb_section_when_level_is_l2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Level L2 also injects Knowledge Context section."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L2")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert "## Knowledge Context" in result.validated_prompt_template


@pytest.mark.unit
def test_kb_evidence_fields_present_in_result_when_level_not_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Result includes all required KB evidence fields when level != 'none'."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L1")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert result.knowledge_context_bundle_hash != ""
    assert result.bundle_level == "L1"
    assert isinstance(result.source_backends_used, list)
    assert isinstance(result.degraded_backends, list)
    assert isinstance(result.injected_context_char_count, int)
    assert result.injected_context_char_count >= 0


@pytest.mark.unit
def test_kb_evidence_fields_empty_when_level_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When level == 'none', KB evidence fields are empty/zero (no context injected)."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="none")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert result.knowledge_context_bundle_hash == ""
    assert result.bundle_level == "none"
    assert result.source_backends_used == []
    assert result.degraded_backends == []
    assert result.injected_context_char_count == 0


@pytest.mark.unit
def test_kb_context_hash_referenced_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No-hidden-context invariant: hash is referenced in the injected context block."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L1")
    result = handler.handle(cmd, existing_task_subjects=[])

    # The hash must appear in the prompt so callers can verify context provenance
    assert result.knowledge_context_bundle_hash in result.validated_prompt_template


@pytest.mark.unit
def test_backward_compat_existing_dispatch_no_kb_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing dispatches (no knowledge_context_level) behave identically to 'none'."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    # Simulate an old-style command without knowledge_context_level (defaults to "none")
    cmd = ModelDispatchWorkerCommand(
        name="old-style-fixer",
        team="test-team",
        role=EnumWorkerRole.fixer,
        scope="Old dispatch style",
        targets=["OMN-0001", "omnimarket#1"],
    )
    result = handler.handle(cmd, existing_task_subjects=[])

    assert result.rejected_reason == ""
    assert "## Knowledge Context" not in result.validated_prompt_template
    assert result.knowledge_context_bundle_hash == ""
    assert result.injected_context_char_count == 0


@pytest.mark.unit
def test_l0_level_produces_minimal_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """L0 injects at least the section header and a hash-referenced bundle."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L0")
    result = handler.handle(cmd, existing_task_subjects=[])

    assert "## Knowledge Context" in result.validated_prompt_template
    assert result.bundle_level == "L0"
    assert result.knowledge_context_bundle_hash != ""


@pytest.mark.unit
def test_injected_char_count_matches_actual_context_length(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """injected_context_char_count matches the actual character count of injected text."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerDispatchWorker()
    cmd = _make_fixer_cmd(knowledge_context_level="L1")
    result = handler.handle(cmd, existing_task_subjects=[])

    # Extract the KB section from the prompt
    prompt = result.validated_prompt_template
    kb_start = prompt.find("## Knowledge Context")
    assert kb_start != -1, "KB section not found"

    # Find the end of the KB section (next ## header or end of string)
    import re

    next_section = re.search(
        r"^## ", prompt[kb_start + len("## Knowledge Context") :], re.MULTILINE
    )
    if next_section:
        kb_end = kb_start + len("## Knowledge Context") + next_section.start()
        kb_text = prompt[kb_start:kb_end]
    else:
        kb_text = prompt[kb_start:]

    assert result.injected_context_char_count == len(kb_text)
