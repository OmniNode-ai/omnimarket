# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15483 — the merge path honors the hold-marker vocabulary that already ships.

Before this change the marker was consumed by exactly one consumer (the OCC
companion-authoring path, OMN-14741 F-17) and the merge path was blind to it: a
PR whose title explicitly said it must not land was landed by the sweep the
moment its required checks went green.

What each class proves
----------------------
``TestSingleHoldVocabulary``
    Acceptance criterion 1 — one definition, not two. Its falsifier is a second,
    independent hold vocabulary anywhere in the tree.

``TestMergeHandlerHonorsHold``
    Criteria 2/4/5 through the REAL ``HandlerPrLifecycleMerge.handle`` with a
    recording adapter, so "refused" means the GitHub merge call was never made —
    not that a helper returned a flag.

``TestOrchestratorFanoutHonorsHold``
    Criterion 3 — the cross-boundary case. Drives the actual sweep path
    (``_call_merge_fanout`` -> real merge handler -> adapter) rather than a mock
    of the matcher, and asserts the producer fills the seam it is supposed to
    fill. Two individually-green units with an unmatched seam would be a silent
    no-op; this is the test that catches it.

Fixtures below deliberately contain literal hold tokens. They live in a test
file on purpose — the marker must never appear in this PR's own title or body,
which would hold the PR that adds the hold gate.

Related:
    - OMN-15483: merge sweep lands PRs inside the adversarial-verification window
    - OMN-14741 F-17: the marker vocabulary this extends
    - OMN-14151: the tri-state (None = unknown, never "clear") idiom reused here
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.pr_arm_gate import EnumArmDecision
from omnimarket.merge_control.hold_marker import (
    HOLD_MARKER_RE,
    EnumMergeHoldStatus,
    evaluate_merge_hold,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
    HandlerPrLifecycleMerge,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command import (
    ModelPrMergeCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    InventoryResult,
    PrRecord,
    TriageRecord,
)

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "omnimarket"
_CANONICAL_MODULE = _SRC_ROOT / "merge_control" / "hold_marker.py"

# A title carrying the marker, and one that does not. The unheld title is the
# regression control: its verdict must be identical before and after this change.
_HELD_TITLE = "[WS4 PARITY PROBE - DO NOT MERGE] gateway probe"
_UNHELD_TITLE = "feat(OMN-8084): create pr lifecycle merge effect"


class _RecordingAdapter:
    """Records every merge call so 'refused' can mean 'never called'."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    async def merge_pr(self, repo: str, pr_number: int, use_merge_queue: bool) -> str:
        self.calls.append({"repo": repo, "pr_number": pr_number})
        return f"auto-merge enabled (squash) for {repo}#{pr_number}"

    async def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        self.comments.append({"repo": repo, "pr_number": pr_number})


class _StubEventBus:
    """Minimal publisher — the merge fan-out publishes nothing itself."""

    _started = True

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, *args: Any, **kwargs: Any) -> None:
        self.published.append((args, kwargs))


class _ArmEverythingGate:
    """Test double returning the bare decision the orchestrator short-circuits on.

    Deliberately permissive: it arms every candidate so that anything the fan-out
    refuses was refused by the hold gate under test, never by the arm-gate.
    """

    async def handle(self, request: Any) -> EnumArmDecision:
        return EnumArmDecision.ARM


def _command(
    *,
    pr_title: str | None,
    pr_labels: tuple[str, ...] | None,
    dry_run: bool = False,
    triage_verdict: str = "green",
) -> ModelPrMergeCommand:
    return ModelPrMergeCommand(
        correlation_id=uuid4(),
        pr_number=5584,
        repo="OmniNode-ai/onex_change_control",
        triage_verdict=triage_verdict,
        use_merge_queue=False,
        pr_title=pr_title,
        pr_labels=pr_labels,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Criterion 1 — one vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSingleHoldVocabulary:
    """The hold vocabulary is defined exactly once in the tree."""

    def test_only_one_module_declares_a_hold_regex(self) -> None:
        """No module outside hold_marker.py may bind a hold regex.

        Falsifier for criterion 1. Re-declaring ``_DO_NOT_MERGE_RE`` in a node —
        which is exactly the state this ticket found, with two divergent copies
        — fails here.
        """
        offenders: list[str] = []
        for path in _SRC_ROOT.rglob("*.py"):
            if path == _CANONICAL_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                call = node.value
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                is_re_compile = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "compile"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "re"
                )
                if not is_re_compile:
                    continue
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    normalized = target.id.lstrip("_").upper()
                    if any(
                        marker in normalized
                        for marker in ("DO_NOT_MERGE", "HOLD_MARKER", "DNM", "WIP")
                    ):
                        offenders.append(f"{path.relative_to(_SRC_ROOT)}:{target.id}")

        assert offenders == [], (
            "a second hold vocabulary is declared outside the canonical module "
            f"(criterion 1 falsifier): {offenders}"
        )

    @pytest.mark.parametrize(
        "token",
        [
            # occ_companion_emitter's original set
            "DO NOT MERGE",
            "DO-NOT-MERGE",
            "DONOTMERGE",
            "WORK IN PROGRESS",
            "[WIP]",
            # handler_occ_companion_compute's original set
            "do not merge",
            "DNM",
            "WIP",
            "[draft",
        ],
    )
    def test_shared_vocabulary_is_a_superset_of_both_predecessors(
        self, token: str
    ) -> None:
        """Promoting to one definition may not un-suppress anything.

        Both former definitions were consumers of suppression, and neither was a
        superset of the other. The shared definition is their union, so every
        token either site suppressed on before still suppresses.
        """
        assert HOLD_MARKER_RE.search(f"chore: {token} probe") is not None

    def test_ordinary_titles_do_not_match(self) -> None:
        """The vocabulary does not fire on unrelated words (no over-blocking)."""
        for benign in (
            "feat(OMN-1234): add merge queue support",
            "fix: do not drop the envelope on retry",
            "chore: swipe left on stale branches",
            "docs: describe the workflow in progress tracking doc",
        ):
            assert HOLD_MARKER_RE.search(benign) is None, benign


# ---------------------------------------------------------------------------
# Criteria 2, 4, 5 — the handler refuses, discriminates, and explains
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMergeHandlerHonorsHold:
    """The real merge handler, with a recording adapter as the merge oracle."""

    async def test_held_title_is_refused_and_never_calls_github(self) -> None:
        """GREEN half of the RED/GREEN pair.

        RED proof (recorded in the PR body): at pre-fix code this same fixture
        reached the adapter and merged — the blindness this ticket describes.
        """
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(_command(pr_title=_HELD_TITLE, pr_labels=()))

        assert result.merged is False
        assert adapter.calls == [], "a held PR reached the GitHub merge call"
        assert result.hold_status == EnumMergeHoldStatus.HELD.value
        assert result.hold_matched_source == "title"

    async def test_held_label_is_refused(self) -> None:
        """The label surface holds too, not only the title."""
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(
            _command(pr_title=_UNHELD_TITLE, pr_labels=("area:ci", "do-not-merge"))
        )

        assert result.merged is False
        assert adapter.calls == []
        assert result.hold_status == EnumMergeHoldStatus.HELD.value
        assert result.hold_matched_source == "label"

    async def test_unreadable_hold_state_is_refused_not_assumed_clear(self) -> None:
        """Criterion 2's explicit falsifier: indeterminate must refuse.

        Neither surface observed — the probe saw nothing at all. Fail-closed
        means this is treated as held; it must never decay to 'clear'.
        """
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(_command(pr_title=None, pr_labels=None))

        assert result.merged is False
        assert adapter.calls == []
        assert result.hold_status == EnumMergeHoldStatus.INDETERMINATE.value
        assert set(result.hold_unobserved_sources) == {"title", "labels"}

    async def test_hold_is_honored_in_dry_run_too(self) -> None:
        """A dry run reports the hold it would honor, not a merge it would do."""
        handler = HandlerPrLifecycleMerge()

        result = await handler.handle(
            _command(pr_title=_HELD_TITLE, pr_labels=(), dry_run=True)
        )

        assert result.merged is False
        assert "[noop]" not in result.merge_action
        assert result.hold_status == EnumMergeHoldStatus.HELD.value

    async def test_skip_reason_names_the_matched_token(self) -> None:
        """Criterion 5: the receipt says WHY, not merely that it was skipped."""
        handler = HandlerPrLifecycleMerge(github_adapter=_RecordingAdapter())

        result = await handler.handle(_command(pr_title=_HELD_TITLE, pr_labels=()))

        assert result.hold_matched_token is not None
        assert result.hold_matched_token.lower().replace("-", " ") == "do not merge"
        assert result.hold_matched_token in result.merge_action

    async def test_a_hold_is_not_counted_as_a_failure(self) -> None:
        """A hold is a correct no-op. Setting ``error`` would inflate prs_failed."""
        handler = HandlerPrLifecycleMerge(github_adapter=_RecordingAdapter())

        result = await handler.handle(_command(pr_title=_HELD_TITLE, pr_labels=()))

        assert result.error is None

    async def test_clearing_the_hold_releases_the_pr(self) -> None:
        """Criterion 4: the gate discriminates rather than blocking everything."""
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(
            _command(pr_title=_UNHELD_TITLE, pr_labels=("area:ci",))
        )

        assert result.merged is True
        assert result.error is None
        assert len(adapter.calls) == 1
        assert result.hold_status == EnumMergeHoldStatus.CLEAR.value

    async def test_unheld_pr_verdict_is_unchanged(self) -> None:
        """Backward-safety control: the unheld path behaves exactly as before.

        Same assertions the pre-existing golden chain makes — merged, no error,
        one adapter call with the right coordinates.
        """
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(_command(pr_title=_UNHELD_TITLE, pr_labels=()))

        assert result.merged is True
        assert result.error is None
        assert "squash" in result.merge_action
        assert adapter.calls[0]["pr_number"] == 5584

    async def test_non_green_verdict_still_rejected_before_the_hold_probe(self) -> None:
        """Ordering: the verdict gate is unchanged and still comes first."""
        adapter = _RecordingAdapter()
        handler = HandlerPrLifecycleMerge(github_adapter=adapter)

        result = await handler.handle(
            _command(pr_title=_UNHELD_TITLE, pr_labels=(), triage_verdict="red")
        )

        assert result.merged is False
        assert result.error is not None
        assert result.hold_status is None
        assert adapter.calls == []


# ---------------------------------------------------------------------------
# Criterion 3 — cross-boundary, through the path that actually runs
# ---------------------------------------------------------------------------


def _inventory(title: str) -> InventoryResult:
    return InventoryResult(
        prs=(
            PrRecord(
                pr_number=5584,
                repo="OmniNode-ai/onex_change_control",
                title=title,
                branch="jonah/probe",
                checks_status="success",
                review_status="approved",
                has_conflicts=False,
                is_draft=False,
                coderabbit_unresolved=0,
                merge_state_status="CLEAN",
            ),
        ),
        total_collected=1,
    )


def _triage() -> tuple[TriageRecord, ...]:
    return (
        TriageRecord(
            pr_number=5584,
            repo="OmniNode-ai/onex_change_control",
            category=EnumPrCategory.GREEN,
        ),
    )


def _orchestrator(adapter: _RecordingAdapter) -> HandlerPrLifecycleOrchestrator:
    return HandlerPrLifecycleOrchestrator(
        merge=HandlerPrLifecycleMerge(github_adapter=adapter),
        arm_gate=_ArmEverythingGate(),
        event_bus=_StubEventBus(),
    )


@pytest.mark.unit
class TestOrchestratorFanoutHonorsHold:
    """The seam: inventory title -> merge command -> hold gate -> no merge."""

    async def test_fanout_does_not_land_a_required_green_held_pr(self) -> None:
        """A green, armed, held PR produces zero merge calls.

        This is the regression proof. Deleting the hold check in the handler
        makes this test go RED — the assertion is on the adapter, which only
        the real code path can reach.
        """
        adapter = _RecordingAdapter()
        orch = _orchestrator(adapter)

        result = await orch._call_merge_fanout(
            correlation_id=uuid4(),
            prs_to_merge=_triage(),
            dry_run=False,
            inv_result=_inventory(_HELD_TITLE),
        )

        assert adapter.calls == [], "the sweep landed a held PR"
        assert result.prs_merged == 0
        assert result.prs_failed == 0, "a hold is a skip, not a failure"

    async def test_fanout_lands_the_same_pr_once_the_hold_is_cleared(self) -> None:
        """Identical fixture minus the marker merges — the gate discriminates."""
        adapter = _RecordingAdapter()
        orch = _orchestrator(adapter)

        result = await orch._call_merge_fanout(
            correlation_id=uuid4(),
            prs_to_merge=_triage(),
            dry_run=False,
            inv_result=_inventory(_UNHELD_TITLE),
        )

        assert len(adapter.calls) == 1
        assert result.prs_merged == 1

    async def test_producer_fills_the_title_seam(self) -> None:
        """The fan-out must actually populate ``pr_title``.

        Without this the handler would see ``None`` for every PR, evaluate
        INDETERMINATE and refuse everything — two individually-correct units
        that together halt the sweep. Asserted on the command the handler
        received, not on the producer's source text.
        """
        received: list[ModelPrMergeCommand] = []

        class _CapturingMerge:
            async def handle(self, command: ModelPrMergeCommand) -> Any:
                received.append(command)

                class _R:
                    merged = False
                    error = None

                return _R()

        orch = HandlerPrLifecycleOrchestrator(
            merge=_CapturingMerge(),
            arm_gate=_ArmEverythingGate(),
            event_bus=_StubEventBus(),
        )

        await orch._call_merge_fanout(
            correlation_id=uuid4(),
            prs_to_merge=_triage(),
            dry_run=False,
            inv_result=_inventory(_HELD_TITLE),
        )

        assert len(received) == 1
        assert received[0].pr_title == _HELD_TITLE
        # Labels are genuinely not carried by PrRecord yet, so the producer
        # reports them UNOBSERVED rather than falsely as "observed and empty".
        assert received[0].pr_labels is None
        assert (
            evaluate_merge_hold(
                title=received[0].pr_title, labels=received[0].pr_labels
            ).status
            is EnumMergeHoldStatus.HELD
        )
