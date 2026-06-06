# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12703: mechanical DoD check execution in node_verification_receipt_generator.

The verification node is the EXECUTION authority for the four EnumCheckType
mechanical checks. These tests exercise the real MechanicalCheckRunner against a
temporary worktree (no network, no gh, no pytest subprocess) and assert:

- each check type runs and produces deterministic structured evidence,
- a failed check produces a deterministic, non-free-text summary,
- the handler aggregates one evidence entry per check and derives overall_pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck

from omnimarket.events.verification import (
    ModelVerificationReceiptRequest,
)
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
    MechanicalCheckRunner,
)


@pytest.mark.unit
class TestMechanicalCheckRunner:
    """Real runner against a temp worktree — one assertion per check type."""

    def test_command_exit_0_pass(self, tmp_path: Path) -> None:
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="trivial command succeeds",
                check="true",
                check_type=EnumCheckType.COMMAND_EXIT_0,
            ),
            str(tmp_path),
        )
        assert passed is True
        assert summary == "command_exit_0 exit_code=0"

    def test_command_exit_0_fail(self, tmp_path: Path) -> None:
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="failing command",
                check="exit 3",
                check_type=EnumCheckType.COMMAND_EXIT_0,
            ),
            str(tmp_path),
        )
        assert passed is False
        assert summary == "command_exit_0 exit_code=3"

    def test_file_exists_pass(self, tmp_path: Path) -> None:
        (tmp_path / "present.txt").write_text("x")
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="file is present",
                check="present.txt",
                check_type=EnumCheckType.FILE_EXISTS,
            ),
            str(tmp_path),
        )
        assert passed is True
        assert "exists=True" in summary

    def test_file_exists_fail(self, tmp_path: Path) -> None:
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="file is missing",
                check="missing.txt",
                check_type=EnumCheckType.FILE_EXISTS,
            ),
            str(tmp_path),
        )
        assert passed is False
        assert "exists=False" in summary

    def test_grep_present_pass(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("MARKER = 1\n")
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="marker present",
                check="-r MARKER .",
                check_type=EnumCheckType.GREP_PRESENT,
            ),
            str(tmp_path),
        )
        assert passed is True
        assert summary == "grep_present found=True"

    def test_grep_present_fail_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("nothing here\n")
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="marker present",
                check="-r MARKER .",
                check_type=EnumCheckType.GREP_PRESENT,
            ),
            str(tmp_path),
        )
        assert passed is False
        assert summary == "grep_present found=False"

    def test_grep_absent_pass(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("clean code\n")
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="no TODO markers",
                check="-r TODO .",
                check_type=EnumCheckType.GREP_ABSENT,
            ),
            str(tmp_path),
        )
        assert passed is True
        assert summary == "grep_absent found=False"

    def test_grep_absent_fail_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("# TODO: fix this\n")
        runner = MechanicalCheckRunner()
        passed, summary = runner.run_check(
            ModelMechanicalCheck(
                criterion="no TODO markers",
                check="-r TODO .",
                check_type=EnumCheckType.GREP_ABSENT,
            ),
            str(tmp_path),
        )
        assert passed is False
        assert summary == "grep_absent found=True"


@pytest.mark.unit
class TestHandlerMechanicalDimension:
    """Handler aggregates one evidence entry per mechanical check."""

    def test_handler_runs_mechanical_checks_and_derives_overall(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "present.txt").write_text("x")
        handler = HandlerVerificationReceiptGenerator()
        request = ModelVerificationReceiptRequest(
            task_id="task-1",
            claim="mechanical checks",
            worktree_path=str(tmp_path),
            verify_ci=False,
            verify_tests=False,
            mechanical_checks=(
                ModelMechanicalCheck(
                    criterion="command ok",
                    check="true",
                    check_type=EnumCheckType.COMMAND_EXIT_0,
                ),
                ModelMechanicalCheck(
                    criterion="file present",
                    check="present.txt",
                    check_type=EnumCheckType.FILE_EXISTS,
                ),
            ),
        )

        receipt = handler.handle(request)

        assert receipt.overall_pass is True
        dims = [c.dimension for c in receipt.checks]
        assert dims == [
            "mechanical_check:command ok",
            "mechanical_check:file present",
        ]
        # Structured details are preserved per check.
        assert receipt.checks[0].details["check_type"] == "command_exit_0"
        assert receipt.checks[1].details["check"] == "present.txt"

    def test_handler_overall_fails_on_any_failed_check(self, tmp_path: Path) -> None:
        handler = HandlerVerificationReceiptGenerator()
        request = ModelVerificationReceiptRequest(
            task_id="task-1",
            claim="mechanical checks",
            worktree_path=str(tmp_path),
            verify_ci=False,
            verify_tests=False,
            mechanical_checks=(
                ModelMechanicalCheck(
                    criterion="command ok",
                    check="true",
                    check_type=EnumCheckType.COMMAND_EXIT_0,
                ),
                ModelMechanicalCheck(
                    criterion="missing file",
                    check="nope.txt",
                    check_type=EnumCheckType.FILE_EXISTS,
                ),
            ),
        )

        receipt = handler.handle(request)

        assert receipt.overall_pass is False
        assert receipt.checks[0].passed is True
        assert receipt.checks[1].passed is False
