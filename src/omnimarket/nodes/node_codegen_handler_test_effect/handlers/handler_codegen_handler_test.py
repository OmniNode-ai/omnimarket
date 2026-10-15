# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: the must-fail acceptance control for a generated handler (OMN-21153).

The factory accepts a generated handler only when its generated test passes the
real handler AND fails a stubbed mutant of it. A test that passes both does not
test anything. This EFFECT owns the subprocess and filesystem I/O of that
control:

* it lays the generated handler out as its top-level package
  (``node_<stem>/handlers/handler_<stem>.py``) with the spec's test module
  beside it, and runs pytest over the test;
* it derives a stubbed mutant of the handler (the handler method's body
  replaced by ``raise NotImplementedError``) and runs the SAME test against it;
* it grades the pair with the same computes the delegated test loop's
  must-fail control uses (``delegated_test_loop/must_fail_control.py``,
  OMN-19361): the run classification is ``digest_pytest_run`` and the verdict
  is ``grade_control``, so an assertion-level failure of the mutant is
  headline, a mutant the test passes is ``control_did_not_fail``, and an
  infrastructure fault is never a pass.

The handler is accepted only when the real run passed and the control grade is
headline. Every other combination — including a missing test source — rejects.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from omnimarket.codegen.models import (
    ModelCodegenHandlerTestCommand,
    ModelCodegenHandlerTestResult,
)
from omnimarket.nodes.node_delegated_test_control_compute import (
    EnumControlStatus,
    ModelControlGradeRequest,
    grade_control,
)
from omnimarket.nodes.node_delegated_test_control_compute.models.model_delegated_test_control import (
    RunOutcome,
)
from omnimarket.nodes.node_pytest_failure_digest_compute import (
    ModelPytestRunReport,
    digest_pytest_run,
)

_TIMEOUT_SECONDS = 120

#: The pytest failure digest's closed outcome vocabulary, as control literals.
_RUN_OUTCOMES: tuple[RunOutcome, ...] = (
    "passed",
    "failed_call",
    "failed_collection",
    "error_setup",
    "no_tests",
    "infra_error",
)


def _as_run_outcome(value: str) -> RunOutcome:
    """Narrow a digest outcome string to the control's literal vocabulary."""
    for candidate in _RUN_OUTCOMES:
        if value == candidate:
            return candidate
    return "infra_error"


def _package_stem(node_name: str) -> str:
    """Snake-case stem of the node class name, without the ``node_`` prefix."""
    chars: list[str] = []
    for index, char in enumerate(node_name):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    stem = "".join(chars)
    return stem[len("node_") :] if stem.startswith("node_") else stem


def _layout(
    root: Path, stem: str, handler_source: str, test_source: str
) -> tuple[Path, Path]:
    """Write the handler package and the test module; return the test path."""
    package = root / f"node_{stem}"
    (package / "handlers").mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "handlers" / "__init__.py").write_text("", encoding="utf-8")
    (package / "handlers" / f"handler_{stem}.py").write_text(
        handler_source, encoding="utf-8"
    )
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    test_path = tests / f"test_handler_{stem}.py"
    test_path.write_text(test_source, encoding="utf-8")
    return root, test_path


def _run_pytest(root: Path, test_path: Path) -> str:
    """Run the test module once; return the digest outcome string."""
    junit = root / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(root)}
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(test_path),
                "-q",
                f"--junitxml={junit}",
            ],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "infra_error"
    report = ModelPytestRunReport(
        junit_xml=junit.read_text(encoding="utf-8") if junit.exists() else "",
        exit_code=completed.returncode,
    )
    return digest_pytest_run(report).outcome.value


def _stub_mutant(source: str, class_name: str, method: str) -> str | None:
    """The handler source with the method's body replaced by a stub raise."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    item.body = [
                        ast.Raise(
                            exc=ast.Call(
                                func=ast.Name(id="NotImplementedError"),
                                args=[],
                                keywords=[],
                            )
                        )
                    ]
                    return ast.unparse(tree)
    return None


def evaluate_handler_test(
    command: ModelCodegenHandlerTestCommand,
) -> ModelCodegenHandlerTestResult:
    """Run the acceptance control and return its verdict (never raises)."""
    spec = command.state.spec
    stem = _package_stem(spec.node_name)
    if not spec.handler_test_source.strip():
        return ModelCodegenHandlerTestResult(
            state=command.state,
            real_outcome="no_tests",
            mutant_outcome="no_tests",
            grade_status=EnumControlStatus.CONTROL_DID_NOT_FAIL.value,
            accepted=False,
            reason="spec carries no handler_test_source; no test to accept on",
        )
    with tempfile.TemporaryDirectory(prefix="codegen_handler_test_") as tmp:
        root = Path(tmp)
        _, test_path = _layout(
            root, stem, command.state.source_text, spec.handler_test_source
        )
        real_outcome = _run_pytest(root, test_path)
        if real_outcome != "passed":
            return ModelCodegenHandlerTestResult(
                state=command.state,
                real_outcome=real_outcome,
                mutant_outcome="no_tests",
                grade_status=EnumControlStatus.CONTROL_DID_NOT_FAIL.value,
                accepted=False,
                reason=(
                    f"generated test did not pass the handler (outcome={real_outcome})"
                ),
            )
        mutant = _stub_mutant(
            command.state.source_text, spec.node_name, spec.handler_method
        )
        if mutant is None:
            return ModelCodegenHandlerTestResult(
                state=command.state,
                real_outcome=real_outcome,
                mutant_outcome="infra_error",
                grade_status=EnumControlStatus.INFRA_ERROR.value,
                accepted=False,
                reason=(
                    f"could not derive a stub mutant: no method "
                    f"{spec.handler_method!r} on class {spec.node_name!r}"
                ),
            )
        mutant_root = root / "mutant"
        _, mutant_test = _layout(mutant_root, stem, mutant, spec.handler_test_source)
        mutant_outcome = _run_pytest(mutant_root, mutant_test)

    grade = grade_control(
        ModelControlGradeRequest(
            fixed_outcome="passed",
            prefix_outcome=_as_run_outcome(mutant_outcome),
            mutation_requested=False,
            prefix_ref_equals_fixed_ref=False,
        )
    )
    return ModelCodegenHandlerTestResult(
        state=command.state,
        real_outcome=real_outcome,
        mutant_outcome=mutant_outcome,
        grade_status=grade.status.value,
        accepted=grade.headline,
        reason=grade.reason,
    )


class HandlerCodegenHandlerTest:
    """EFFECT: accept a generated handler only through its must-fail control."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def handle(
        self, command: ModelCodegenHandlerTestCommand
    ) -> ModelCodegenHandlerTestResult:
        return evaluate_handler_test(command)


__all__ = ["HandlerCodegenHandlerTest", "evaluate_handler_test"]
