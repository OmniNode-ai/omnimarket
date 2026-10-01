# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic correctness fixtures; no labelled lab content belongs here."""

import ast
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.delegation.rubric.contract_loader import (
    load_delegation_class_rubrics,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_code_generation import (
    ids_traceable,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    added_diff_lines,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelClassRubric,
    ModelIdsTraceableParams,
    ModelRubricCheckRequest,
    ModelRubricExecutionResult,
    ModelRubricVerdict,
)

pytestmark = pytest.mark.unit
DIFF = """diff --git a/src/widget.py b/src/widget.py
--- a/src/widget.py
+++ b/src/widget.py
@@ -10,4 +10,6 @@
 def build_widget():
-    old_value = 99
+    widget_value = 42
+    return widget_value
     # synthetic context
+    # another line
     # end
"""


def request(
    task_class="code_review",
    answer="",
    prompt="Review this change",
    source=DIFF,
    results=(),
):
    contract = load_delegation_class_rubrics()
    rubric = contract.for_class(task_class)
    return ModelRubricCheckRequest(
        task_class=task_class,
        request_text=prompt,
        answer_text=answer,
        source_text=source,
        rubric=rubric,
        execution_results=results,
    )


def criterion(criterion_id, **kwargs):
    verdict = HandlerDelegationRubricCheck().handle(request(**kwargs))
    return next(row for row in verdict.criteria if row.criterion_id == criterion_id)


@pytest.mark.parametrize(
    ("answer", "outcome", "reason"),
    [
        ("FINDING src/widget.py:11 `widget_value = 42`", "PASS", "citations_verified"),
        ("src/absent.py:11 `widget_value = 42`", "FAIL", "path_not_in_context"),
        ("src/widget.py:400 `widget_value = 42`", "FAIL", "line_outside_hunks"),
        ("src/widget.py:11 `imaginary_value = 88`", "FAIL", "quote_not_found"),
        ("src/widget.py:11-400", "FAIL", "line_outside_hunks"),
        ("src/widget.py:11 `old_value = 99`", "PASS", "citations_verified"),
        ("There are no observations.", "UNDETERMINED", "no_citations"),
    ],
)
def test_cited_lines_exist_diff(answer, outcome, reason):
    row = criterion("cited_lines_exist", answer=answer)
    assert row.outcome == outcome
    assert row.reason_code == reason


def test_cited_lines_exist_no_line_map():
    row = criterion(
        "cited_lines_exist", answer="src/widget.py:11", source="widget_value = 42"
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_line_map")


def test_cited_lines_exist_no_findings():
    row = criterion(
        "cited_lines_exist",
        answer="NO FINDINGS",
        prompt="Return exactly `NO FINDINGS` if clear.",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "no_findings_declared")
    assert (
        criterion("cited_lines_exist", answer="NO FINDINGS").outcome == "UNDETERMINED"
    )


def test_cited_lines_exist_quotes_belong_to_file():
    other = "\ndiff --git a/src/other.py b/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n+other_value = 81\n"
    assert (
        criterion(
            "cited_lines_exist",
            answer="src/widget.py:11 `other_value = 81`",
            source=DIFF + other,
        ).reason_code
        == "quote_not_found"
    )


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ("Use `widget_value` and `build_widget()`.", "PASS"),
        ("Use `ImaginaryWidget`.", "FAIL"),
        ("Use `None` or `dict`.", "UNDETERMINED"),
        ("A short explanation.", "UNDETERMINED"),
    ],
)
def test_named_symbols_exist(answer, outcome):
    assert criterion("named_symbols_exist", answer=answer).outcome == outcome


@pytest.mark.parametrize(
    ("answer", "outcome", "reason"),
    [
        (
            "FINDING | src/widget.py:11 | high | explanation | `widget_value = 42`",
            "PASS",
            "format_verified",
        ),
        (
            "FINDING | src/widget.py:11 | high | explanation `widget_value = 42`",
            "FAIL",
            "format_violation",
        ),
        (
            "FINDING | src/widget.py:11 | critical | explanation | `widget_value = 42`",
            "FAIL",
            "format_violation",
        ),
        (
            "FINDING | src/widget.py:11 | high | explanation | no quote",
            "FAIL",
            "format_violation",
        ),
    ],
)
def test_declared_format_met_findings(answer, outcome, reason):
    prompt = "Return FINDING | path:line | severity(high|medium|low) | explanation | `verbatim quoted text`"
    row = criterion("declared_format_met", answer=answer, prompt=prompt)
    assert (row.outcome, row.reason_code) == (outcome, reason)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [("Prose only", "FAIL"), ('{"ok": true}', "PASS"), ("```json\n{}\n```", "FAIL")],
)
def test_declared_format_met_json(answer, outcome):
    assert (
        criterion(
            "declared_format_met", answer=answer, prompt="Return JSON only"
        ).outcome
        == outcome
    )


def test_declared_format_met_json_fences_allowed():
    assert (
        criterion(
            "declared_format_met",
            answer="```json\n{}\n```",
            prompt="Return JSON; fences allowed",
        ).outcome
        == "PASS"
    )


@pytest.mark.parametrize(
    "token",
    [
        "SYN-999",
        "98001",
        '"missing phrase here"',
        "src/absent.py",
        "`absent_token`",
        "widget#777",
    ],
)
def test_claims_traceable_missing(token):
    sentence = f"The summary contains {token}."
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        answer=sentence,
        source='SYN-101 delivered 12000 widgets with "carefully tested parts".',
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "sentence_untraceable")
    assert sentence in row.detail
    assert row.facts


def test_claims_traceable_faithful():
    source = "SYN-101 delivered 12000 widgets with carefully tested parts. widget#123 edits src/widget.py and widget_value."
    answer = 'SYN-101 delivered 12,000 widgets with "carefully tested parts".\n- widget#123 edits `widget_value` in src/widget.py.'
    assert (
        criterion(
            "claims_traceable", task_class="summarization", answer=answer, source=source
        ).outcome
        == "PASS"
    )


def test_claims_traceable_no_items():
    assert (
        criterion(
            "claims_traceable",
            task_class="summarization",
            answer="Everything improved.",
            source="Things changed.",
        ).outcome
        == "UNDETERMINED"
    )


@pytest.mark.parametrize(
    ("answer", "outcome"), [("SYN-101", "FAIL"), ("SYN-101 SYN-102", "PASS")]
)
def test_id_coverage(answer, outcome):
    row = criterion(
        "id_coverage",
        task_class="summarization",
        prompt="Summarize all ticket ids",
        source="SYN-101 and SYN-102 are commitments.",
        answer=answer,
    )
    assert row.outcome == outcome


def test_id_coverage_not_requested():
    assert (
        criterion(
            "id_coverage",
            task_class="summarization",
            prompt="Summarize briefly",
            source="SYN-101",
            answer="Done",
        ).reason_code
        == "not_applicable"
    )


@pytest.mark.parametrize(
    ("answer", "outcome", "reason"),
    [
        ("```python\ndef widget():\n    return 1\n```", "PASS", "code_parsed"),
        ("```python\ndef widget(:\n```", "FAIL", "syntax_error"),
        ("```yaml\nvalue: [broken\n```", "FAIL", "syntax_error"),
        ('```json\n{"value":}\n```', "FAIL", "syntax_error"),
        ("```rust\nfn widget() {}\n```", "UNDETERMINED", "language_unsupported"),
        ("Only an explanation.", "UNDETERMINED", "no_code"),
        ('{"code": "def widget(:"}', "FAIL", "syntax_error"),
    ],
)
def test_code_parses(answer, outcome, reason):
    row = criterion(
        "code_parses",
        task_class="code_generation",
        prompt="Write Python code",
        answer=answer,
    )
    assert (row.outcome, row.reason_code) == (outcome, reason)


def test_code_parses_no_fence():
    assert (
        criterion(
            "code_parses",
            task_class="test",
            prompt="Write Python code without fences",
            answer="def widget(:",
        ).outcome
        == "FAIL"
    )


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ("```yaml\n# file: contract.yaml\nname: widget\n```", "PASS"),
        (
            "```yaml\n# SPDX-License-Identifier: MIT\n# file: contract.yaml\nname: widget\n```",
            "FAIL",
        ),
        ("```python\nx = 1\n```", "FAIL"),
        ("```yaml\n# file: contract.yaml\n```\n```yaml\nname: widget\n```", "FAIL"),
    ],
)
def test_declared_format_met_code(answer, outcome):
    prompt = 'Return one fenced yaml block with first line "# file: contract.yaml"'
    assert (
        criterion(
            "declared_format_met",
            task_class="code_generation",
            prompt=prompt,
            answer=answer,
        ).outcome
        == outcome
    )


@pytest.mark.parametrize(
    ("passed", "outcome", "reason"),
    [
        (True, "PASS", "tests_verified"),
        (False, "FAIL", "test_failed"),
        (None, "UNDETERMINED", "no_execution_result"),
    ],
)
@pytest.mark.parametrize(
    ("task_class", "criterion_id"),
    [("code_review", "named_test_passes"), ("test", "stated_test_passes")],
)
def test_stated_test_passes(passed, outcome, reason, task_class, criterion_id):
    results = (
        ()
        if passed is None
        else (
            ModelRubricExecutionResult(
                target="tests/test_widget.py::test_widget", passed=passed
            ),
        )
    )
    row = criterion(
        criterion_id,
        task_class=task_class,
        answer="Run tests/test_widget.py::test_widget",
        results=results,
    )
    assert (row.outcome, row.reason_code) == (outcome, reason)


def test_stated_test_passes_request_target():
    row = criterion(
        "stated_test_passes",
        task_class="test",
        answer="Done",
        prompt="Run test_widget",
        results=(ModelRubricExecutionResult(target="test_widget", passed=False),),
    )
    assert row.reason_code == "test_failed"


def test_ids_traceable():
    assert (
        criterion(
            "ids_traceable", task_class="test", answer="# SYN-999", source="SYN-101"
        ).reason_code
        == "invented_id"
    )


def test_undecidable_never_pass():
    handler = HandlerDelegationRubricCheck()
    empty = handler.handle(
        request(answer="Nothing noteworthy", source="Unstructured context")
    )
    assert empty.outcome == "UNDETERMINED"
    partial = handler.handle(request(answer="`widget_value`"))
    assert partial.outcome == "PASS"
    assert sum(row.outcome == "PASS" for row in partial.criteria) == 1
    failed = handler.handle(request(answer="`widget_value` src/absent.py:11"))
    assert failed.outcome == "FAIL"
    assert failed.failed_criteria == ("cited_lines_exist",)
    with pytest.raises(ValidationError):
        ModelRubricVerdict.model_validate({**empty.model_dump(), "outcome": "PASS"})


def test_unknown_class():
    verdict = HandlerDelegationRubricCheck().handle(
        request(task_class="planning", answer="Done")
    )
    assert verdict.outcome == "UNDETERMINED"
    assert verdict.criteria[0].reason_code == "no_rubric_for_class"


def test_contract_loads():
    contract = load_delegation_class_rubrics()
    assert set(contract.classes) == {
        "code_review",
        "review",
        "summarization",
        "code_generation",
        "test",
        "tool_use",
    }
    assert (
        contract.for_class("review").criteria
        == contract.for_class("code_review").criteria
    )
    assert all(
        row.params.model_dump()
        for rubric in contract.classes.values()
        for row in rubric
    )
    with pytest.raises(ValidationError):
        ModelClassRubric.model_validate(
            {
                "rubric_version": "v1",
                "task_class": "test",
                "criteria": [
                    {
                        "criterion_id": "code_parses",
                        "description": "Parse",
                        "params": {},
                    }
                ],
            }
        )


def test_deterministic():
    item = request(answer="src/widget.py:11 `widget_value = 42`")
    assert HandlerDelegationRubricCheck().handle(
        item
    ) == HandlerDelegationRubricCheck().handle(item)


def test_no_model_call():
    import omnimarket.nodes.node_delegation_rubric_check_compute.handlers as package

    forbidden = {
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "aiohttp",
        "openai",
        "anthropic",
        "kafka",
        "pathlib",
    }
    for path in Path(package.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom):
                    names.append(node.module or "")
                assert not any(
                    name.split(".")[0] in forbidden
                    or name.startswith(("omnimarket.inference", "omnimarket.adapters"))
                    or "ModelEventEnvelope" in name
                    or "ModelHandlerOutput" in name
                    for name in names
                )
            if isinstance(node, ast.Call):
                assert not (
                    isinstance(node.func, ast.Name) and node.func.id in {"open", "Path"}
                )
                assert not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"read_text", "read_bytes", "now", "utcnow"}
                )
    assert not inspect.iscoroutinefunction(HandlerDelegationRubricCheck.handle)


def test_no_literal_thresholds():
    import omnimarket.nodes.node_delegation_rubric_check_compute.handlers as package

    for path in Path(package.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Compare):
                assert not any(
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, (int, float))
                    and child.value not in {0, 1}
                    for child in ast.walk(node)
                )
    item = request(answer="src/widget.py:17")
    handler = HandlerDelegationRubricCheck()
    assert handler.handle(item).outcome == "FAIL"
    rows = list(item.rubric.criteria)
    rows[0] = rows[0].model_copy(
        update={"params": rows[0].params.model_copy(update={"line_tolerance": 2})}
    )
    tolerant = item.model_copy(
        update={"rubric": item.rubric.model_copy(update={"criteria": tuple(rows)})}
    )
    assert handler.handle(tolerant).outcome == "PASS"
    item = request(answer="`widget_value` `missing_symbol`")
    assert handler.handle(item).outcome == "FAIL"
    rows = list(item.rubric.criteria)
    rows[1] = rows[1].model_copy(
        update={
            "params": rows[1].params.model_copy(update={"max_missing_fraction": 0.5})
        }
    )
    tolerant = item.model_copy(
        update={"rubric": item.rubric.model_copy(update={"criteria": tuple(rows)})}
    )
    assert handler.handle(tolerant).outcome == "PASS"


def test_cited_lines_exist_removed_old_range():
    diff = "diff --git a/src/widget.py b/src/widget.py\n--- a/src/widget.py\n+++ b/src/widget.py\n@@ -40,2 +10,2 @@\n-old_value = 99\n+widget_value = 42\n # end\n"
    assert (
        criterion(
            "cited_lines_exist", source=diff, answer="src/widget.py:40 `old_value = 99`"
        ).outcome
        == "PASS"
    )
    assert (
        criterion(
            "cited_lines_exist",
            source=diff,
            answer="src/widget.py:40 `widget_value = 42`",
        ).reason_code
        == "line_outside_hunks"
    )


def test_cited_lines_exist_extensionless_path():
    diff = "diff --git a/Makefile b/Makefile\n+++ b/Makefile\n@@ -1 +1 @@\n+synthetic_target: dependency\n"
    assert (
        criterion(
            "cited_lines_exist",
            source=diff,
            answer="Makefile:1 `synthetic_target: dependency`",
        ).outcome
        == "PASS"
    )


def test_cited_lines_exist_zero_new_range():
    diff = "diff --git a/src/widget.py b/src/widget.py\n+++ b/src/widget.py\n@@ -40,1 +10,0 @@\n-old_value = 99\n"
    assert (
        criterion("cited_lines_exist", source=diff, answer="src/widget.py:10").outcome
        == "FAIL"
    )
    assert (
        criterion(
            "cited_lines_exist", source=diff, answer="src/widget.py:40 `old_value = 99`"
        ).outcome
        == "PASS"
    )


def test_cited_lines_exist_short_quote_ignored():
    assert (
        criterion("cited_lines_exist", answer="src/widget.py:11 `absent`").outcome
        == "PASS"
    )


def test_named_symbols_exist_attribute_and_acronym():
    assert (
        criterion(
            "named_symbols_exist",
            source="obj.widget_value HTTPServer",
            answer="`widget_value` `HTTPServer`",
        ).outcome
        == "PASS"
    )
    assert (
        criterion(
            "named_symbols_exist", source="Something else", answer="`HTTPServer`"
        ).outcome
        == "FAIL"
    )


@pytest.mark.parametrize("answer", ["SYN-10", "widget#12"])
def test_claims_traceable_identifier_boundary(answer):
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        source="SYN-101 widget#123",
        answer=answer,
    )
    assert row.reason_code == "sentence_untraceable"


def test_claims_traceable_all_facts_bounded():
    item = request(
        task_class="summarization",
        answer="SYN-201. SYN-202. SYN-203.",
        source="SYN-101",
    )
    rows = list(item.rubric.criteria)
    rows[0] = rows[0].model_copy(
        update={"params": rows[0].params.model_copy(update={"max_facts": 2})}
    )
    verdict = HandlerDelegationRubricCheck().handle(
        item.model_copy(
            update={"rubric": item.rubric.model_copy(update={"criteria": tuple(rows)})}
        )
    )
    assert verdict.criteria[0].facts == ("SYN-201", "SYN-202")


def test_claims_traceable_table_and_list():
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        source="SYN-101",
        answer="- SYN-101 delivered\n| SYN-999 | complete |",
    )
    assert "| SYN-999 | complete |" in row.detail


def test_source_none_and_empty_are_distinct():
    assert (
        criterion(
            "claims_traceable",
            task_class="summarization",
            source=None,
            prompt="SYN-101",
            answer="SYN-101",
        ).outcome
        == "PASS"
    )
    assert (
        criterion(
            "claims_traceable",
            task_class="summarization",
            source="",
            prompt="SYN-101",
            answer="SYN-101",
        ).outcome
        == "FAIL"
    )


def test_stated_test_passes_failure_overrides_missing():
    row = criterion(
        "stated_test_passes",
        task_class="test",
        answer="test_widget test_other",
        results=(ModelRubricExecutionResult(target="test_widget", passed=False),),
    )
    assert row.reason_code == "test_failed"


def test_stated_test_passes_conflicting_evidence_fails():
    rows = (
        ModelRubricExecutionResult(target="test_widget", passed=True),
        ModelRubricExecutionResult(target="test_widget", passed=False),
    )
    assert (
        criterion(
            "stated_test_passes", task_class="test", answer="test_widget", results=rows
        ).reason_code
        == "test_failed"
    )


def test_stated_test_passes_no_cross_target_match():
    row = criterion(
        "stated_test_passes",
        task_class="test",
        answer="tests/test_widget.py::test_widget",
        results=(
            ModelRubricExecutionResult(
                target="tests/test_other.py::test_widget", passed=True
            ),
        ),
    )
    assert row.reason_code == "no_execution_result"


def test_code_parses_mixed_unsupported_never_passes():
    assert (
        criterion(
            "code_parses",
            task_class="test",
            answer="```python\nx = 1\n```\n```rust\nfn x() {}\n```",
        ).outcome
        == "UNDETERMINED"
    )


def test_code_parses_reports_line():
    row = criterion(
        "code_parses", task_class="test", answer="```python\nx = 1\ndef bad(:\n```"
    )
    assert row.detail == "line 2"


def test_code_parses_yaml_and_json_pass():
    assert (
        criterion(
            "code_parses",
            task_class="test",
            answer='```yaml\nvalue: 1\n```\n```json\n{"value": 1}\n```',
        ).outcome
        == "PASS"
    )


def test_request_rejects_wrong_class_rubric():
    item = request()
    with pytest.raises(ValidationError):
        ModelRubricCheckRequest.model_validate(
            {**item.model_dump(), "task_class": "test"}
        )


def test_contract_loads_explicit_path():
    path = Path("src/omnimarket/configs/delegation_class_rubrics.v1.yaml")
    assert load_delegation_class_rubrics(path) == load_delegation_class_rubrics()


def test_code_parses_json_embedded_yaml():
    assert (
        criterion(
            "code_parses",
            task_class="test",
            prompt="Write YAML",
            answer='{"code": "value: [broken"}',
        ).reason_code
        == "syntax_error"
    )


def test_code_parses_empty_unfenced_is_no_code():
    assert (
        criterion(
            "code_parses",
            task_class="test",
            prompt="Write Python without fences",
            answer="",
        ).reason_code
        == "no_code"
    )


def test_declared_format_met_json_explicit_fence():
    assert (
        criterion(
            "declared_format_met",
            task_class="test",
            prompt="Return JSON in one fenced json block",
            answer="```json\n{}\n```",
        ).outcome
        == "PASS"
    )


def test_cited_lines_exist_quote_shared_between_old_and_new():
    diff = "diff --git a/src/widget.py b/src/widget.py\n+++ b/src/widget.py\n@@ -40 +10 @@\n-widget_value = 99\n+widget_value = 42\n"
    assert (
        criterion(
            "cited_lines_exist", source=diff, answer="src/widget.py:10 `widget_value`"
        ).outcome
        == "PASS"
    )


def test_models_are_frozen_and_forbid_extra():
    item = request()
    with pytest.raises(ValidationError):
        item.answer_text = "Changed"
    with pytest.raises(ValidationError):
        ModelRubricCheckRequest.model_validate(
            {**item.model_dump(), "unknown": "value"}
        )


def test_cited_lines_exist_quote_outside_hunk_is_rejected():
    diff = "diff --git a/src/widget.py b/src/widget.py\n+++ b/src/widget.py\n@@ -1 +1 @@\n+widget_value = 42\n\n+outside_value = 81\n"
    assert (
        criterion(
            "cited_lines_exist",
            source=diff,
            answer="src/widget.py:1 `outside_value = 81`",
        ).reason_code
        == "quote_not_found"
    )


def test_cited_lines_exist_removed_text_looks_like_header():
    diff = "diff --git a/src/widget.py b/src/widget.py\n+++ b/src/widget.py\n@@ -40 +10 @@\n--- synthetic removed\n+widget_value = 42\n"
    assert (
        criterion(
            "cited_lines_exist",
            source=diff,
            answer="src/widget.py:40 `-- synthetic removed`",
        ).outcome
        == "PASS"
    )


REFLOW_DIFF = """diff --git a/src/gate.py b/src/gate.py
--- a/src/gate.py
+++ b/src/gate.py
@@ -20,2 +20,5 @@
 def narrow(expected, event_name):
+    if event_name != "push":
+        expected = tuple(
+            name for name in expected if name in ALLOWED_NAMES
+        )
"""


def test_cited_lines_exist_quote_reflowed_across_lines_is_found():
    answer = (
        "src/gate.py:22 `expected = tuple(name for name in expected "
        "if name in ALLOWED_NAMES)`"
    )
    row = criterion("cited_lines_exist", answer=answer, source=REFLOW_DIFF)
    assert (row.outcome, row.reason_code) == ("PASS", "citations_verified")


def test_named_symbols_exist_ignores_python_builtins_and_all_caps_words():
    answer = "Raises `NotADirectoryError` or `StopIteration`; the `INSERT` runs once."
    row = criterion("named_symbols_exist", answer=answer)
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_candidate_symbols")


def test_named_symbols_exist_builtins_switch_comes_from_contract():
    contract = load_delegation_class_rubrics()
    rubric = contract.for_class("code_review")
    criteria = tuple(
        row.model_copy(
            update={
                "params": row.params.model_copy(
                    update={"ignore_python_builtins": False}
                )
            }
        )
        if row.criterion_id == "named_symbols_exist"
        else row
        for row in rubric.criteria
    )
    verdict = HandlerDelegationRubricCheck().handle(
        ModelRubricCheckRequest(
            task_class="code_review",
            request_text="Review this change",
            answer_text="Raises `NotADirectoryError`.",
            source_text=DIFF,
            rubric=rubric.model_copy(update={"criteria": criteria}),
        )
    )
    row = next(r for r in verdict.criteria if r.criterion_id == "named_symbols_exist")
    assert (row.outcome, row.reason_code) == ("FAIL", "symbol_not_in_context")


def test_declared_format_met_one_block_per_file_is_not_one_block():
    prompt = (
        "Write two Python files. Return one fenced python block per file; "
        'first line of each block is "# file: <relative path>".'
    )
    answer = (
        "```python\n# file: pkg/a.py\nA = 1\n```\n\n"
        "```python\n# file: pkg/b.py\nB = 2\n```\n"
    )
    row = criterion(
        "declared_format_met",
        task_class="code_generation",
        prompt=prompt,
        answer=answer,
        source=None,
    )
    assert (row.outcome, row.reason_code) == ("PASS", "format_verified")


def test_declared_format_met_placeholder_first_line_still_requires_the_shape():
    prompt = 'Return one fenced python block per file; first line of each block is "# file: <name>.py".'
    answer = "```python\n# SPDX-License-Identifier: MIT\n# file: a.py\nA = 1\n```\n"
    row = criterion(
        "declared_format_met",
        task_class="code_generation",
        prompt=prompt,
        answer=answer,
        source=None,
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "format_violation")


def test_id_coverage_not_triggered_by_a_bare_all():
    prompt = (
        "Summarize this row in 3 sentences. Row: SYN-11 related SYN-12; "
        "8 files all 0600."
    )
    row = criterion(
        "id_coverage",
        task_class="summarization",
        prompt=prompt,
        answer="The row concerns SYN-11.",
        source=None,
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


def test_id_coverage_triggered_by_every_commitment():
    prompt = (
        "Extract every concrete commitment below. SYN-11 builds it; SYN-12 gates it."
    )
    row = criterion(
        "id_coverage",
        task_class="summarization",
        prompt=prompt,
        answer="1. build it (SYN-11)",
        source=None,
    )
    assert (row.outcome, row.reason_code, row.facts) == (
        "FAIL",
        "id_missing_from_answer",
        ("SYN-12",),
    )


def test_claims_traceable_bare_ref_matches_a_qualified_source_ref():
    source = "widget#41 lands first, then widget#42."
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        answer="- widget#41 must land before #42.",
        source=source,
    )
    assert (row.outcome, row.reason_code) == ("PASS", "claims_verified")


def test_claims_traceable_bare_ref_absent_from_source_fails():
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        answer="- widget#41 must land before #77.",
        source="widget#41 lands first, then widget#42.",
    )
    assert (row.outcome, row.reason_code, row.facts) == (
        "FAIL",
        "sentence_untraceable",
        ("#77",),
    )


def test_claims_traceable_list_numbers_are_not_claims():
    answer = "\n".join(f"{n}. build SYN-11" for n in range(40, 44))
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        answer=answer,
        source="SYN-11 must be built.",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "claims_verified")


def test_claims_traceable_qualified_ref_matches_a_bare_source_ref():
    row = criterion(
        "claims_traceable",
        task_class="summarization",
        answer="- widget#42 merged.",
        source="widget#41 and #42 merged.",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "claims_verified")


def _diff(context, removed, added_lines):
    body = "".join("+" + line + "\n" for line in added_lines)
    return (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1,3 +1,4 @@\n"
        " " + context + "\n"
        "-" + removed + "\n" + body
    )


def test_ids_traceable_context_line_id_not_scored():
    row = criterion(
        "ids_traceable",
        task_class="code_generation",
        answer=_diff("# see OMN-9001", "old_line = 1", ["x = 1"]),
        prompt="Write the code",
        source="Some context",
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


def test_ids_traceable_removed_line_id_not_scored():
    row = criterion(
        "ids_traceable",
        task_class="code_generation",
        answer=_diff("context = 0", "# OMN-9002 old", ["x = 2"]),
        prompt="Write the code",
        source="Some context",
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


def test_ids_traceable_added_line_invented_id_fails():
    row = criterion(
        "ids_traceable",
        task_class="code_generation",
        answer=_diff("# see OMN-9001", "old_line = 1", ["# OMN-9003 new"]),
        prompt="Write the code for OMN-9001",
        source="Context about OMN-9001",
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "invented_id")
    assert "OMN-9003" in row.detail
    assert "OMN-9003" in row.facts
    assert "OMN-9001" not in row.detail
    assert "OMN-9001" not in row.facts


def test_ids_traceable_added_line_id_in_request_passes():
    row = criterion(
        "ids_traceable",
        task_class="code_generation",
        answer=_diff("context = 0", "old_line = 1", ["# OMN-9004"]),
        prompt="Implement OMN-9004",
        source="Some context",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "ids_verified")
    assert row.facts == ("OMN-9004",)


def test_ids_traceable_non_diff_answer_still_scored_whole():
    row = criterion(
        "ids_traceable",
        task_class="code_generation",
        answer="# SYN-999",
        prompt="Write the code",
        source="SYN-101",
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "invented_id")


def test_ids_traceable_whole_answer_scope_still_scores_context():
    rubric = load_delegation_class_rubrics().for_class("code_generation")
    row = next(r for r in rubric.criteria if r.criterion_id == "ids_traceable")
    whole = row.model_copy(
        update={
            "params": ModelIdsTraceableParams(
                ticket_id_pattern=row.params.ticket_id_pattern,
                diff_scope="whole_answer",
            )
        }
    )
    item = request(
        task_class="code_generation",
        answer=_diff("# see OMN-9005", "old_line = 1", ["x = 1"]),
        prompt="Write the code",
        source="Some context",
    )
    result = ids_traceable(item, whole)
    assert (result.outcome, result.reason_code) == ("FAIL", "invented_id")


@pytest.mark.parametrize("task_class", ["code_generation", "test"])
def test_ids_traceable_contract_declares_added_lines(task_class):
    rubric = load_delegation_class_rubrics().for_class(task_class)
    row = next(r for r in rubric.criteria if r.criterion_id == "ids_traceable")
    assert row.params.diff_scope == "added_lines"


def test_ids_traceable_params_require_diff_scope():
    with pytest.raises(ValidationError):
        ModelIdsTraceableParams(ticket_id_pattern="x")


def test_added_diff_lines_parser():
    assert added_diff_lines("plain prose only") is None
    two_added = added_diff_lines(
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,4 @@\n context line\n-removed line\n+a\n+b\n more context\n"
    )
    assert two_added == "a\nb"
    file_headers = added_diff_lines(
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,4 @@\n context\n-removed\n+a\n"
        "diff --git a/g.py b/g.py\n--- a/g.py\n+++ b/g.py\n"
        "@@ -1,2 +1,3 @@\n context\n-removed\n+b\n"
    )
    assert file_headers == "a\nb"
    ignored = added_diff_lines(
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,4 @@\n # OMN-9001\n-# OMN-9002\n+x = 1\n"
    )
    assert ignored == "x = 1"
    interrupted = added_diff_lines(
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,3 +1,4 @@\n context\n+a\nSome prose line\n+b\n"
        "```\n+c\n"
        "@@ -2,2 +2,2 @@\n context\n+d\n"
    )
    assert interrupted == "a\nd"
