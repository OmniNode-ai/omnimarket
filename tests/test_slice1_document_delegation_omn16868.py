# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Slice 1 (OMN-16868): document-class delegation on the delegated-fix path.

OMN-13940 shipped Slice 0 — a deterministic, zero-LLM ruff path pinned to
``_DELEGATION_MODEL_NAME = "ruff-deterministic"``. This module pins the Slice 1
swap the node contract already specifies (``contract.yaml:28-30``): a
docstring/comment-only diff routes to a real
``HandlerDelegateSkill(task_type="document")`` call instead of ruff, with the
command/result shape unchanged.

Every LLM call is faked at the ADAPTER boundary
(``ProtocolDocumentDelegationRunner``) — this module performs zero network I/O.
The live counterpart is ``test_live_document_delegation_omn16868.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixCommand,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_acceptance_telemetry import (
    JsonlAcceptanceTelemetryRecorder,
    ModelDelegatedFixAttemptRecord,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.diff_classifier import (
    is_docstring_comment_only_change,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
    _DELEGATION_MODEL_NAME,
    DocumentDelegationOutcome,
    HandlerDelegatedFix,
    PrPolishRunOutcome,
)

# ---------------------------------------------------------------------------
# Fakes — every adapter seam the handler injects
# ---------------------------------------------------------------------------


class _FakeWorktreeResolver:
    def __init__(self, path: Path) -> None:
        self._path = path

    def resolve(self, **kwargs: object) -> Path:
        return self._path


class _RecordingRuffRunner:
    def __init__(self) -> None:
        self.called = False

    def run(self, worktree: Path) -> None:
        self.called = True


class _RecordingDocumentRunner:
    """Fakes the delegation call at the adapter boundary — no network."""

    def __init__(
        self,
        *,
        delegation_model: str = "qwen3.8",
        cost_usd: float = 0.0,
        backend_id: str = "local-heavy-reasoning",
        tier: str = "local",
    ) -> None:
        self.called = False
        self.seen_changed_files: list[str] | None = None
        self._outcome = DocumentDelegationOutcome(
            delegation_model=delegation_model,
            cost_usd=cost_usd,
            backend_id=backend_id,
            tier=tier,
        )

    async def run(
        self, worktree: Path, *, changed_files: list[str]
    ) -> DocumentDelegationOutcome:
        self.called = True
        self.seen_changed_files = list(changed_files)
        return self._outcome


class _FakeGitDiffAdapter:
    def __init__(self, *, files: list[str] | None = None, lines: int = 6) -> None:
        self._files = files if files is not None else ["src/foo.py"]
        self._lines = lines
        self.discarded = False

    def changed_files(self, worktree: Path) -> list[str]:
        return list(self._files)

    def diff_line_count(self, worktree: Path) -> int:
        return self._lines

    def commit_all(self, worktree: Path, message: str) -> str:
        self.last_message = message
        return "deadbeef"

    def discard_changes(self, worktree: Path) -> None:
        self.discarded = True


class _FakePrPolishRunner:
    def __init__(self, *, phase: str = "done") -> None:
        self.kwargs: dict[str, object] = {}
        self._phase = phase

    def run(self, **kwargs: object) -> PrPolishRunOutcome:
        self.kwargs = kwargs
        return PrPolishRunOutcome(final_phase=self._phase, error_message=None)


class _StaticClassifier:
    def __init__(self, verdict: bool) -> None:
        self._verdict = verdict

    def is_document_class(self, worktree: Path, *, changed_files: list[str]) -> bool:
        return self._verdict


class _MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelDelegatedFixAttemptRecord] = []

    def record(self, record: ModelDelegatedFixAttemptRecord) -> None:
        self.records.append(record)


def _make_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/somewhere\n")
    return worktree


def _command(worktree: Path, **overrides: object) -> ModelDelegatedFixCommand:
    payload: dict[str, object] = {
        "correlation_id": uuid4(),
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 777,
        "ticket_id": "OMN-16868",
        "block_reason": "code_failure",
        "changed_files": ["src/foo.py"],
        "diff_total_lines": 6,
        "worktree_path": str(worktree),
        "requested_at": datetime.now(tz=UTC),
    }
    payload.update(overrides)
    return ModelDelegatedFixCommand(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Routing: document-class -> delegation, everything else -> ruff
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDocumentDelegationRouting:
    async def test_document_class_diff_calls_delegation_not_ruff(
        self, tmp_path: Path
    ) -> None:
        """The Slice 1 swap: a docstring/comment diff must NOT run ruff."""
        worktree = _make_worktree(tmp_path)
        ruff = _RecordingRuffRunner()
        document = _RecordingDocumentRunner()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=ruff,
            document_delegation_runner=document,
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert result.outcome == EnumDelegatedFixOutcome.ACCEPTED
        assert document.called, "document-class diff must route to delegation"
        assert not ruff.called, "ruff must be SWAPPED OUT, not run alongside"

    async def test_non_document_diff_keeps_the_slice0_ruff_path(
        self, tmp_path: Path
    ) -> None:
        """Non-docstring diffs are unchanged from Slice 0 — ruff, zero cost."""
        worktree = _make_worktree(tmp_path)
        ruff = _RecordingRuffRunner()
        document = _RecordingDocumentRunner()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=ruff,
            document_delegation_runner=document,
            diff_classifier=_StaticClassifier(False),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert result.outcome == EnumDelegatedFixOutcome.ACCEPTED
        assert ruff.called
        assert not document.called
        assert result.delegation_model == _DELEGATION_MODEL_NAME
        assert result.cost_usd == 0.0

    async def test_result_stamps_the_real_delegation_model_and_cost(
        self, tmp_path: Path
    ) -> None:
        """``delegation_model``/``cost_usd`` come from the delegation response."""
        worktree = _make_worktree(tmp_path)
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(
                delegation_model="qwen3.8", cost_usd=0.0
            ),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert result.delegation_model == "qwen3.8"
        assert result.delegation_model != _DELEGATION_MODEL_NAME
        assert result.cost_usd == 0.0

    async def test_delegated_by_trailer_names_the_delegating_model(
        self, tmp_path: Path
    ) -> None:
        """Safety: the commit trailer must attribute the ACTUAL model."""
        worktree = _make_worktree(tmp_path)
        git = _FakeGitDiffAdapter()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(
                delegation_model="qwen3.8"
            ),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=git,
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        await handler.handle(_command(worktree))

        assert "delegated-by: qwen3.8" in git.last_message

    async def test_no_document_runner_falls_back_to_ruff(self, tmp_path: Path) -> None:
        """Absent a document runner the node behaves exactly as Slice 0."""
        worktree = _make_worktree(tmp_path)
        ruff = _RecordingRuffRunner()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=ruff,
            document_delegation_runner=None,
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert ruff.called
        assert result.delegation_model == _DELEGATION_MODEL_NAME


# ---------------------------------------------------------------------------
# The eligibility gate is UNTOUCHED — asserted, not asserted-in-prose
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEligibilityGateUntouched:
    def test_gate_constants_unchanged(self) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
            delegation_eligibility as gate,
        )

        assert gate.MAX_DELEGATION_FILES == 3
        assert gate.MAX_DELEGATION_LINES == 60
        assert gate.TWO_STRIKE_THRESHOLD == 2

    def test_document_delegation_does_not_widen_eligible_block_reasons(self) -> None:
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
            delegation_eligibility as gate,
        )

        eligible, reason = gate.is_delegation_eligible(
            block_reason="receipt_failure",
            changed_files=["src/foo.py"],
            diff_total_lines=1,
            strikes=0,
        )
        assert not eligible
        assert reason == "block_reason_not_eligible:receipt_failure"

    async def test_document_path_still_refuses_an_oversized_actual_diff(
        self, tmp_path: Path
    ) -> None:
        """Defense-in-depth size re-check applies to the delegated diff too."""
        worktree = _make_worktree(tmp_path)
        git = _FakeGitDiffAdapter(files=["a.py", "b.py", "c.py", "d.py"], lines=999)
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=git,
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert result.outcome == EnumDelegatedFixOutcome.REFUSED_SIZE_GATE
        assert git.discarded, "a refused LLM diff must be rolled back, never committed"

    async def test_document_path_still_refuses_a_denylisted_actual_diff(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        git = _FakeGitDiffAdapter(files=["src/auth_session.py"], lines=4)
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=git,
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=_MemoryRecorder(),
        )

        result = await handler.handle(_command(worktree))

        assert result.outcome == EnumDelegatedFixOutcome.REFUSED_DENYLIST
        assert git.discarded

    async def test_no_automerge_stays_set_on_the_delegated_path(
        self, tmp_path: Path
    ) -> None:
        """pr_polish re-entry is unchanged: it never self-arms auto-merge."""
        import inspect

        from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.handler_delegated_fix import (
            LivePrPolishRunner,
        )

        source = inspect.getsource(LivePrPolishRunner)
        assert '"--no-automerge"' in source
        assert '"--skip-repair-dispatch"' in source


# ---------------------------------------------------------------------------
# Acceptance telemetry — the 70% / 20-sample bar must be measurable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAcceptanceTelemetry:
    async def test_every_terminal_outcome_records_one_attempt(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        recorder = _MemoryRecorder()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=recorder,
        )

        await handler.handle(_command(worktree))

        assert len(recorder.records) == 1
        record = recorder.records[0]
        assert record.outcome == EnumDelegatedFixOutcome.ACCEPTED.value
        assert record.accepted is True
        assert record.delegation_model == "qwen3.8"
        assert record.task_type == "document"
        assert record.backend_id == "local-heavy-reasoning"
        assert record.tier == "local"
        assert record.repo == "OmniNode-ai/omnimarket"
        assert record.pr_number == 777

    async def test_a_refusal_is_recorded_as_a_non_accepted_sample(
        self, tmp_path: Path
    ) -> None:
        """Refusals count toward the denominator — otherwise 70% is unfalsifiable."""
        worktree = _make_worktree(tmp_path)
        recorder = _MemoryRecorder()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(files=["x.py"], lines=999),
            pr_polish_runner=_FakePrPolishRunner(),
            acceptance_recorder=recorder,
        )

        await handler.handle(_command(worktree))

        assert len(recorder.records) == 1
        assert recorder.records[0].accepted is False
        assert recorder.records[0].outcome == (
            EnumDelegatedFixOutcome.REFUSED_SIZE_GATE.value
        )

    async def test_a_gate_failure_is_recorded_as_non_accepted(
        self, tmp_path: Path
    ) -> None:
        worktree = _make_worktree(tmp_path)
        recorder = _MemoryRecorder()
        handler = HandlerDelegatedFix(
            worktree_resolver=_FakeWorktreeResolver(worktree),
            ruff_runner=_RecordingRuffRunner(),
            document_delegation_runner=_RecordingDocumentRunner(),
            diff_classifier=_StaticClassifier(True),
            git_diff_adapter=_FakeGitDiffAdapter(),
            pr_polish_runner=_FakePrPolishRunner(phase="failed"),
            acceptance_recorder=recorder,
        )

        await handler.handle(_command(worktree))

        assert recorder.records[0].accepted is False
        assert recorder.records[0].outcome == EnumDelegatedFixOutcome.GATE_FAILED.value

    def test_jsonl_recorder_appends_one_line_per_attempt(self, tmp_path: Path) -> None:
        recorder = JsonlAcceptanceTelemetryRecorder(state_dir=tmp_path)
        for index in range(3):
            recorder.record(
                ModelDelegatedFixAttemptRecord(
                    correlation_id=uuid4(),
                    repo="OmniNode-ai/omnimarket",
                    pr_number=index,
                    block_reason="code_failure",
                    task_type="document",
                    delegation_model="qwen3.8",
                    backend_id="local-heavy-reasoning",
                    tier="local",
                    outcome="accepted",
                    accepted=True,
                    cost_usd=0.0,
                    files_changed=1,
                    lines_changed=4,
                    recorded_at=datetime.now(tz=UTC),
                )
            )

        path = tmp_path / "delegated_fix" / "acceptance_telemetry.jsonl"
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 3
        parsed = json.loads(lines[0])
        assert parsed["task_type"] == "document"
        assert parsed["accepted"] is True
        assert parsed["backend_id"] == "local-heavy-reasoning"

    def test_recorder_never_breaks_a_delegation_on_a_write_failure(
        self, tmp_path: Path
    ) -> None:
        """Telemetry is observability, not a gate — a sink outage must not raise."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        recorder = JsonlAcceptanceTelemetryRecorder(state_dir=blocked)
        recorder.record(
            ModelDelegatedFixAttemptRecord(
                correlation_id=uuid4(),
                repo="r",
                pr_number=1,
                block_reason="code_failure",
                task_type="document",
                delegation_model="qwen3.8",
                backend_id="local-heavy-reasoning",
                tier="local",
                outcome="accepted",
                accepted=True,
                cost_usd=0.0,
                files_changed=1,
                lines_changed=1,
                recorded_at=datetime.now(tz=UTC),
            )
        )

    def test_acceptance_rate_is_computable_from_the_sink(self, tmp_path: Path) -> None:
        """The 70%/20-sample bar must be mechanically checkable off the file."""
        recorder = JsonlAcceptanceTelemetryRecorder(state_dir=tmp_path)
        for index in range(20):
            recorder.record(
                ModelDelegatedFixAttemptRecord(
                    correlation_id=uuid4(),
                    repo="OmniNode-ai/omnimarket",
                    pr_number=index,
                    block_reason="code_failure",
                    task_type="document",
                    delegation_model="qwen3.8",
                    backend_id="local-heavy-reasoning",
                    tier="local",
                    outcome="accepted" if index < 15 else "gate_failed",
                    accepted=index < 15,
                    cost_usd=0.0,
                    files_changed=1,
                    lines_changed=4,
                    recorded_at=datetime.now(tz=UTC),
                )
            )

        samples = recorder.read_samples(task_type="document")
        assert len(samples) == 20
        rate = sum(1 for s in samples if s.accepted) / len(samples)
        assert rate == pytest.approx(0.75)
        # The OMN-13940 widening bar, as two separate mechanical checks.
        assert len(samples) >= 20
        assert rate >= 0.70


# ---------------------------------------------------------------------------
# Diff classifier — refusal-by-default
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDocstringCommentClassifier:
    def test_docstring_only_change_is_document_class(self) -> None:
        before = '''def f(x):
    """Old."""
    return x + 1
'''
        after = '''def f(x):
    """New and much better prose."""
    return x + 1
'''
        assert is_docstring_comment_only_change(before, after) is True

    def test_comment_only_change_is_document_class(self) -> None:
        before = "def f(x):\n    # old note\n    return x + 1\n"
        after = "def f(x):\n    # a clearer note\n    return x + 1\n"
        assert is_docstring_comment_only_change(before, after) is True

    def test_logic_change_is_not_document_class(self) -> None:
        before = '''def f(x):
    """Doc."""
    return x + 1
'''
        after = '''def f(x):
    """Doc."""
    return x + 2
'''
        assert is_docstring_comment_only_change(before, after) is False

    def test_logic_change_hidden_behind_a_docstring_edit_is_not_document_class(
        self,
    ) -> None:
        before = '''def f(x):
    """Old."""
    return x + 1
'''
        after = '''def f(x):
    """New."""
    return x + 99
'''
        assert is_docstring_comment_only_change(before, after) is False

    def test_syntax_error_refuses(self) -> None:
        assert is_docstring_comment_only_change("def f(:\n", "def f(:\n") is False

    def test_added_statement_is_not_document_class(self) -> None:
        before = "def f():\n    return 1\n"
        after = "def f():\n    print('side effect')\n    return 1\n"
        assert is_docstring_comment_only_change(before, after) is False
