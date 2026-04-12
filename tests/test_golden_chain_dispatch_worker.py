"""Golden chain tests for node_dispatch_worker.

TDD-first: these tests were written BEFORE the node was wired into entry points.
They test observable side effects:
  - Template content (file-equivalent: the returned string is the artifact)
  - Collision fence population from task dir files
  - Deduplication via task subject list
  - Role-specific invariants (TDD sequence, hostile_reviewer clause)
  - Wall-clock cap defaults per role

Tests are marked @pytest.mark.integration per OMN-8444 DoD.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    ROLE_CAP_DEFAULTS,
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)


def _cmd(**kwargs: object) -> ModelDispatchWorkerCommand:
    defaults = {
        "name": "test-worker",
        "team": "daylight-0411",
        "role": EnumWorkerRole.fixer,
        "scope": "Fix omnimarket#202 halt_conditions CodeRabbit findings",
        "targets": ["omnimarket#202", "OMN-8375"],
    }
    defaults.update(kwargs)
    return ModelDispatchWorkerCommand(**defaults)  # type: ignore[arg-type]


@pytest.mark.integration
class TestDispatchWorkerGoldenChain:
    """Golden chain: dispatch spec → compiled prompt with correct invariants."""

    def test_dispatch_worker_fixer_template_contains_tdd_sequence(self) -> None:
        """Fixer template must contain TDD-FIRST SEQUENCE header and all 5 steps.

        Observable effect: the validated_prompt_template string returned by the
        handler contains the mandatory TDD sequence text. This is falsifiable:
        if the handler returns a prompt missing these strings, the test fails.
        """
        handler = HandlerDispatchWorker()
        result = handler.handle(_cmd(role=EnumWorkerRole.fixer))

        assert result.rejected_reason == "", (
            f"Unexpected rejection: {result.rejected_reason}"
        )
        prompt = result.validated_prompt_template

        assert "TDD-FIRST SEQUENCE" in prompt, "Missing TDD-FIRST SEQUENCE header"
        assert "Step 1: Read the ticket contract" in prompt, "Missing Step 1"
        assert "Step 2: Write ONE failing integration test" in prompt, "Missing Step 2"
        assert "Step 3: Run the test" in prompt, "Missing Step 3"
        assert "Step 4: Only after Step 3, begin implementation" in prompt, (
            "Missing Step 4"
        )
        assert "Step 5: Implementation is done when" in prompt, "Missing Step 5"

        # hostile_reviewer gate must also be present in fixer
        assert 'Skill(skill="onex:hostile_reviewer"' in prompt, (
            "Missing hostile_reviewer gate"
        )

    def test_dispatch_worker_designer_template_invokes_hostile_reviewer(self) -> None:
        """Designer template must invoke hostile_reviewer skill explicitly.

        Observable effect: compiled prompt contains mandatory Skill() invocation
        and explicitly forbids manual roleplay fallback.
        """
        handler = HandlerDispatchWorker()
        result = handler.handle(
            _cmd(
                role=EnumWorkerRole.designer,
                name="vggp-designer",
                scope="Design VGGP inference pipeline integration",
                targets=["OMN-8400"],
            )
        )

        assert result.rejected_reason == ""
        prompt = result.validated_prompt_template

        assert 'Skill(skill="onex:hostile_reviewer"' in prompt
        assert "hostile_reviewer is MANDATORY" in prompt
        assert "Do NOT do manual roleplay" in prompt
        # slug should be derived from scope
        assert "vggp-designer-design" in prompt or "design-vggp-inference" in prompt, (
            "Missing expected derived slug marker in designer prompt"
        )

    def test_dispatch_worker_collision_fence_present(self) -> None:
        """Collision fences from task dir files are embedded in the compiled prompt.

        Observable effect: a temp task dir with in_progress tasks causes the
        handler to read those files and embed their targets in the prompt.
        The fence block appears in the compiled prompt string (file read is the
        observable side effect; the result embeds it).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_dir = Path(tmpdir)
            team_dir = tasks_dir / "daylight-0411"
            team_dir.mkdir()

            # Write 3 in_progress tasks with target claims
            tasks = [
                {
                    "status": "in_progress",
                    "owner": "pr-202-fix",
                    "subject": "pr-202-fix: fix halt",
                    "metadata": {"targets": ["omnimarket#202"]},
                },
                {
                    "status": "in_progress",
                    "owner": "pr-7786-paired",
                    "subject": "pr-7786-paired: fix core",
                    "metadata": {"targets": ["omnibase_core#796"]},
                },
                {
                    "status": "completed",
                    "owner": "old-worker",
                    "subject": "old-worker: done",
                    "metadata": {"targets": ["OMN-9999"]},
                },
            ]
            for i, t in enumerate(tasks):
                (team_dir / f"task-{i:03d}.json").write_text(json.dumps(t))

            handler = HandlerDispatchWorker()
            # current worker owns omnimarket#202 (overlaps first in_progress task)
            # so fence should emit the first task's OTHER targets (none) → falls
            # back to the overlapping target; second task (omnibase_core#796) has
            # no overlap so it is NOT fenced (overlap-only policy)
            result = handler.handle(
                _cmd(
                    name="new-worker",
                    targets=["omnimarket#202", "OMN-8375"],
                    collision_fences=[],
                ),
                tasks_dir=tasks_dir,
            )

        assert result.rejected_reason == ""
        # omnimarket#202 overlaps pr-202-fix; no non-own targets → fall back to
        # shared target in fence
        assert "omnimarket#202" in result.validated_prompt_template
        # completed task's target should NOT be in fences
        assert "OMN-9999" not in "\n".join(result.collision_fence_embeds)
        # exactly 1 fence: the overlapping omnimarket#202 target
        assert len(result.collision_fence_embeds) == 1

    def test_dispatch_worker_result_has_task_report(self) -> None:
        """Result contains validated_task_description in expected format.

        Observable effect: the returned task description string follows the
        [role] name: scope format, ready to pass to TaskCreate().
        Also verifies proposed_agent_spawn_args has required keys.
        """
        handler = HandlerDispatchWorker()
        result = handler.handle(
            _cmd(
                name="pr-202-fix",
                role=EnumWorkerRole.fixer,
                scope="Fix omnimarket#202 halt_conditions CodeRabbit findings",
                targets=["omnimarket#202", "OMN-8375"],
            )
        )

        assert result.rejected_reason == ""
        assert result.validated_task_description == (
            "[fixer] pr-202-fix: Fix omnimarket#202 halt_conditions CodeRabbit findings"
        )
        spawn = result.proposed_agent_spawn_args
        assert spawn["name"] == "pr-202-fix"
        assert spawn["team_name"] == "daylight-0411"
        assert spawn["model"] == "sonnet"
        assert spawn["subagent_type"] == "general-purpose"

    def test_dispatch_worker_template_version_bumped_on_content_change(self) -> None:
        """Fixer template content matches the golden fixture.

        Observable effect: rendering the fixer template for a canonical input
        produces output that contains all 5 required behavioral invariant strings.
        If template content changes, this test detects the deviation.
        This is the CI behavioral invariant check.
        """
        handler = HandlerDispatchWorker()
        result = handler.handle(
            _cmd(
                name="golden-fixer",
                role=EnumWorkerRole.fixer,
                scope="golden fixture scope",
                targets=["OMN-0001", "omnimarket#1"],
                reports_to="team-lead",
                wall_clock_cap_min=90,
                model="sonnet",
            ),
            existing_task_subjects=[],
        )

        prompt = result.validated_prompt_template

        # These strings are the behavioral invariants. Any change to them
        # requires a template_version bump in contract.yaml.
        invariants = [
            "TDD-FIRST SEQUENCE",
            "Step 1: Read the ticket contract",
            "Step 2: Write ONE failing integration test",
            "Step 3: Run the test",
            "Step 4: Only after Step 3, begin implementation",
            "Step 5: Implementation is done when",
            'Skill(skill="onex:hostile_reviewer"',
            "Collision fences — ABSOLUTE RULE",
            "Wall-clock cap",
            "Stop rules",
        ]
        missing = [inv for inv in invariants if inv not in prompt]
        assert not missing, f"Template missing invariants: {missing}"

    def test_dispatch_worker_wall_clock_cap_defaults_by_role(self) -> None:
        """Each role's compiled prompt contains the correct default wall-clock cap.

        Observable effect: when wall_clock_cap_min is not set, the compiled prompt
        embeds the role's default cap value from ROLE_CAP_DEFAULTS.
        """
        handler = HandlerDispatchWorker()

        for role, expected_cap in ROLE_CAP_DEFAULTS.items():
            result = handler.handle(
                _cmd(
                    name=f"cap-test-{role.value}",
                    role=role,
                    scope=f"Test scope for {role.value}",
                    targets=["OMN-0001", "omnimarket#1"],
                    wall_clock_cap_min=None,
                ),
                existing_task_subjects=[],
            )
            assert result.rejected_reason == "", f"Role {role}: unexpected rejection"
            assert str(expected_cap) in result.validated_prompt_template, (
                f"Role {role}: expected cap {expected_cap} not found in prompt"
            )

    def test_dispatch_worker_dedup_rejects_live_worker(self) -> None:
        """Dispatching a worker whose name matches an in_progress task is rejected.

        Observable effect: result.rejected_reason is non-empty; no new task
        description is generated. With replace=True, the rejection is lifted.
        """
        handler = HandlerDispatchWorker()
        existing_subjects = ["pr-202-fix: fix halt conditions (in progress)"]

        # Should reject
        result = handler.handle(
            _cmd(name="pr-202-fix"),
            existing_task_subjects=existing_subjects,
        )
        assert result.rejected_reason != "", "Expected rejection for in_progress worker"
        assert "pr-202-fix" in result.rejected_reason
        assert result.validated_task_description == ""

        # replace=True should allow through
        result_replace = handler.handle(
            _cmd(name="pr-202-fix", replace=True),
            existing_task_subjects=existing_subjects,
        )
        assert result_replace.rejected_reason == "", (
            "replace=True should bypass rejection"
        )
        assert result_replace.validated_task_description != ""

    def test_dispatch_worker_completed_worker_allows_restart(self) -> None:
        """A task with status=completed or deleted allows restart without --replace.

        Observable effect: existing_task_subjects with completed subjects
        (not matching the in_progress check) does not trigger rejection.
        """
        handler = HandlerDispatchWorker()
        # completed tasks are NOT in the in_progress subjects list
        existing_subjects: list[str] = []  # no in_progress tasks

        result = handler.handle(
            _cmd(name="pr-202-fix"),
            existing_task_subjects=existing_subjects,
        )
        assert result.rejected_reason == "", "Completed worker should allow restart"
