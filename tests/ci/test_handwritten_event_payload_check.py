# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fixture may not hand-write the payload its producer emits, and the gate proves it ran."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_handwritten_event_payload_check_effect.handlers import (
    handler_handwritten_event_payload_check as gate,
)
from omnimarket.nodes.node_handwritten_event_payload_check_effect.models.model_handwritten_event_payload_check import (
    ModelHandwrittenEventPayloadCheckRequest,
)
from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# The eight-key form of a fifteen-key attempt record: it omits exactly the keys a
# stale fixture omitted, and stays green against any producer.
_EIGHT_KEY_ATTEMPT = """
def _attempt(*, tier, passed, score, decision, reason):
    return {
        "tier": tier,
        "backend_id": f"backend-{tier}",
        "model_id": f"model-{tier}",
        "quality_gate_passed": passed,
        "quality_score": score,
        "cost_usd": 0.0,
        "acceptance_decision": decision,
        "acceptance_reason": reason,
    }
"""

_MULTILINE_KWARG = """
def test_projects_the_event(handler):
    handler.handle(
        payload={
            "run_id": "r1",
            "status": "ok",
            "tokens": 3,
        },
    )
"""

_PRODUCER_DERIVED = """
from producer.models import ModelDone


def _payload():
    return ModelDone(run_id="r1", status="ok", tokens=3).model_dump()
"""

_ROW_DICT = """
def test_row_shape():
    row = {"run_id": "r1", "status": "ok", "tokens": 3}
    assert row
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path


def _run(
    root: Path,
    *,
    fence: tuple[tuple[str, str, int], ...] = (),
    minimum: int = 1,
):
    return gate.check(root, fence=fence, minimum=minimum)


class TestAc1TheValidatorRefusesAHandWrittenPayload:
    def test_eight_key_attempt_is_refused_and_its_keys_are_named(self, tmp_path):
        root = _tree(tmp_path, {"tests/test_omn18889_seam.py": _EIGHT_KEY_ATTEMPT})
        result = _run(root)
        assert result.exit_code == 1
        assert result.violations == 1
        text = "\n".join(result.output_lines)
        assert "tests/test_omn18889_seam.py:3 _attempt:ret:_attempt" in text
        for key in ("tier", "backend_id", "acceptance_reason"):
            assert key in text
        assert "failure_class" not in text  # the omitted keys are not in the fixture

    def test_a_multiline_call_is_found_where_a_line_regex_is_not(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/test_x.py": _MULTILINE_KWARG})
        assert _run(root).violations == 1

    def test_the_parser_finds_the_sites(self):
        hard, soft = gate.find_sites(_EIGHT_KEY_ATTEMPT + _MULTILINE_KWARG, "t.py")
        assert [s.site for s in hard] == [
            "_attempt:ret:_attempt",
            "test_projects_the_event:kw:payload",
        ]
        assert soft == []

    def test_a_producer_derived_fixture_passes(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/test_ok.py": _PRODUCER_DERIVED})
        result = _run(root)
        assert result.exit_code == 0
        assert result.violations == 0

    def test_a_row_dict_is_report_only_never_blocking(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/test_row.py": _ROW_DICT})
        result = _run(root)
        assert result.exit_code == 0
        assert "report-only: 1 ambiguous site(s) in 1 file(s)" in result.output_lines[0]

    def test_a_file_outside_the_scope_is_not_read(self, tmp_path):
        root = _tree(
            tmp_path,
            {"tests/unrelated/test_x.py": _EIGHT_KEY_ATTEMPT, "tests/chains/a.py": ""},
        )
        result = _run(root)
        assert result.exit_code == 0
        assert result.scanned_files == 1


class TestAc2TheFenceIsAFenceNotABaseline:
    _ROW = ("tests/projection/test_x.py", "test_projects_the_event:kw:payload", 1)

    def test_a_fenced_site_is_held(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/test_x.py": _MULTILINE_KWARG})
        result = _run(root, fence=(self._ROW,))
        assert result.exit_code == 0
        assert result.violations == 0

    def test_a_fenced_row_whose_file_is_deleted_is_a_hard_failure(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/other.py": ""})
        result = _run(root, fence=(self._ROW,))
        assert result.exit_code == 1
        text = "\n".join(result.output_lines)
        assert "stale fence" in text
        assert "tests/projection/test_x.py" in text

    def test_a_fixed_site_still_listed_is_a_hard_failure(self, tmp_path):
        root = _tree(tmp_path, {"tests/projection/test_x.py": _PRODUCER_DERIVED})
        assert _run(root, fence=(self._ROW,)).exit_code == 1

    def test_a_second_site_in_a_fenced_file_is_a_new_violation(self, tmp_path):
        root = _tree(
            tmp_path,
            {"tests/projection/test_x.py": _MULTILINE_KWARG + _MULTILINE_KWARG},
        )
        result = _run(root, fence=(self._ROW,))
        assert result.exit_code == 1
        assert result.violations == 1

    def test_every_live_fenced_row_still_matches_the_tree(self):
        result = gate.check(ROOT, fence=gate._FENCED, minimum=1)
        assert "stale fence" not in "\n".join(result.output_lines)
        assert result.exit_code == 0


class TestAc3TheScanProvesItRan:
    def test_a_tree_below_the_floor_fails_naming_the_floor(self, tmp_path):
        root = _tree(tmp_path, {"tests/chains/a.py": ""})
        result = _run(root, minimum=gate.DEFAULT_MIN_EXPECTED_FILES)
        assert result.exit_code == 1
        text = "\n".join(result.output_lines)
        assert f"floor {gate.DEFAULT_MIN_EXPECTED_FILES}" in text
        assert "scan floor" in text

    def test_the_live_tree_passes_at_or_above_the_floor(self, capsys):
        result = gate.HandlerHandwrittenEventPayloadCheck().handle(
            ModelHandwrittenEventPayloadCheckRequest(repo_root=str(ROOT))
        )
        assert result.exit_code == 0, result.output_lines
        assert result.scanned_files >= gate.DEFAULT_MIN_EXPECTED_FILES
        assert result.violations == 0
        assert f"{result.scanned_files} file(s) scanned" in result.output_lines[0]

    def test_the_live_tree_without_its_fence_fails(self):
        """Positive control: the fence is what holds the live tree, not a blind scan."""
        result = gate.check(ROOT, fence=(), minimum=1)
        assert result.exit_code == 1
        assert result.violations > 0


class TestAc4TheGateIsWiredToARequiredContext:
    def test_lint_is_a_strict_ci_summary_job(self):
        assert "lint" in STRICT_GATE_JOBS

    def test_the_lint_job_runs_the_node(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        steps = workflow["jobs"]["lint"]["steps"]
        runs = [step.get("run", "") for step in steps]
        assert any(
            "python -m omnimarket.nodes.node_handwritten_event_payload_check_effect.runtime_handwritten_event_payload_check"
            in run
            for run in runs
        )
        assert any(
            "tests/ci/test_handwritten_event_payload_check.py" in run for run in runs
        )

    def test_the_precommit_hook_runs_the_node(self):
        config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
        hooks = {h["id"]: h for repo in config["repos"] for h in repo["hooks"]}
        hook = hooks["handwritten-event-payload"]
        assert (
            "omnimarket.nodes.node_handwritten_event_payload_check_effect"
            in hook["entry"]
        )
        assert hook["pass_filenames"] is False

    def test_the_node_is_registered_as_an_entry_point(self):
        text = (ROOT / "pyproject.toml").read_text()
        assert (
            'node_handwritten_event_payload_check_effect = "omnimarket.nodes.node_handwritten_event_payload_check_effect"'
            in text
        )
