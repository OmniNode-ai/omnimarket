# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node returns what the copy it replaces returned, cell by cell, on a recorded input (OMN-22989).

``fixtures/recorded_internal_copy.json`` holds planted registry trees (overlay, sources declaration
and every file) and what the private copy of the completeness check printed on each: the exit code,
every finding line and the summary line. The node runs the same trees through its own reading and
checking halves and the two outputs are compared per cell: exit code, each finding's kind, subject
and detail, each count of the summary. A control plants a divergence in the recorded side and in the
node's side and requires the comparison to name it.

One recorded case is a declared divergence, not a parity case: the node's installer scan ignores a
path token that carries a wildcard (OMN-20804), the private copy never did, so it reads a plugin
pruner's list of unit-directory globs as an installer. That case is asserted as exactly that one
finding the node does not raise.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers import (
    HandlerAutomationLivenessOverlayCheck,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers.handler_automation_liveness_installer_scan import (
    classify_source,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    EnumAutomationLivenessSourceClass,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_effect.handlers import (
    HandlerAutomationLivenessOverlayCheckRun,
)

pytestmark = pytest.mark.unit

# The run handler's own request type, read from its signature: the effect node's models package is
# another node's, and the cross-node reach-in ratchet refuses a test here importing from it.
ModelAutomationLivenessOverlayCheckRunRequest = get_type_hints(
    HandlerAutomationLivenessOverlayCheckRun.handle
)["request"]

_FIXTURE = Path(__file__).parent / "fixtures" / "recorded_internal_copy.json"
_PRIVATE = "omnibase_private"
_OVERLAY = f"{_PRIVATE}/src/{_PRIVATE}/overlays/automation_liveness_overlay.yaml"
_SOURCES = f"{_PRIVATE}/src/{_PRIVATE}/overlays/automation_liveness_sources.yaml"
_GLOB_DIVERGENCE = "known_divergence_glob_list_pruner"
_OPENS_A_FINDING = re.compile(r"^(?:[a-z_]+: |automation-liveness overlay: )")
_PYDANTIC_DOCS = re.compile(r"errors\.pydantic\.dev/[0-9.]+/")

Cells = dict[str, Any]


def _recorded() -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return recorded


def _parity_cases() -> list[dict[str, Any]]:
    return [case for case in _recorded() if case["name"] != _GLOB_DIVERGENCE]


def _case(name: str) -> dict[str, Any]:
    return next(case for case in _recorded() if case["name"] == name)


def _materialize(case: dict[str, Any], root: Path) -> None:
    files = {
        _OVERLAY: case["overlay"],
        _SOURCES: case["sources"],
        **case["files"],
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "omnibase_infra").mkdir(exist_ok=True)


def _run_node(case: dict[str, Any], root: Path) -> dict[str, Any]:
    """What the node prints on the recorded tree, in the recorded shape."""
    _materialize(case, root)
    result = HandlerAutomationLivenessOverlayCheckRun().handle(
        ModelAutomationLivenessOverlayCheckRunRequest(
            internal_root=root / _PRIVATE,
            registry_root=root,
            internal_only=case["internal_only"],
        )
    )
    return {
        "exit_code": result.exit_code,
        "stdout": list(result.stdout_lines),
        "stderr": list(result.stderr_lines),
    }


def _cells(output: dict[str, Any]) -> Cells:
    """One cell per fact: the exit code, each finding's three fields and each summary count."""
    cells: Cells = {"exit_code": output["exit_code"], "stderr": tuple(output["stderr"])}
    printed = _PYDANTIC_DOCS.sub(
        "errors.pydantic.dev/VERSION/", "\n".join(output["stdout"])
    )
    finding_lines: list[str] = []
    for line in printed.splitlines():
        # A line that opens no "<kind>: " or summary continues the finding above it (a
        # validation error spans lines).
        if finding_lines and not _OPENS_A_FINDING.match(line):
            finding_lines[-1] += f"\n{line}"
        else:
            finding_lines.append(line)
    summary = finding_lines.pop() if finding_lines else ""
    for index, line in enumerate(finding_lines):
        kind, _, rest = line.partition(": ")
        subject, _, detail = rest.partition(": ")
        cells[f"finding[{index}].kind"] = kind
        cells[f"finding[{index}].subject"] = subject
        cells[f"finding[{index}].detail"] = detail
    cells["finding_count"] = len(finding_lines)
    counts = re.fullmatch(
        r"automation-liveness overlay: (\d+) entries, (\d+) sources found in (\d+) "
        r"repositories, (\d+) findings, scope (.+)",
        summary,
    )
    cells["summary"] = counts.groups() if counts else summary
    return cells


def divergence(recorded: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every cell whose recorded value and node value differ, named."""
    left, right = _cells(recorded), _cells(actual)
    return [
        f"{cell}: recorded={left.get(cell)!r} node={right.get(cell)!r}"
        for cell in sorted({*left, *right})
        if left.get(cell) != right.get(cell)
    ]


@pytest.mark.parametrize("case", _parity_cases(), ids=lambda case: case["name"])
def test_parity_with_internal_copy_cell_by_cell(
    case: dict[str, Any], tmp_path: Path
) -> None:
    assert divergence(case["recorded"], _run_node(case, tmp_path)) == []


def test_parity_with_internal_copy_recorded_input_has_every_finding_kind() -> None:
    kinds = {
        value
        for case in _parity_cases()
        for cell, value in _cells(case["recorded"]).items()
        if cell.endswith(".kind")
    }

    assert kinds == {
        "overlay_invalid",
        "positive_control_mismatch",
        "sources_invalid",
        "repository_not_scanned",
        "undeclared_source",
        "stale_source",
        "undeclared_unit",
        "not_installed_without_installer",
        "retired_still_installed",
    }


def test_parity_with_internal_copy_covers_every_source_class_of_the_installer_scan() -> (
    None
):
    scanned = {
        found.source_class
        for case in _parity_cases()
        for path, text in case["files"].items()
        if "/" in path
        and (
            found := classify_source(path.split("/", 1)[0], path.split("/", 1)[1], text)
        )
        is not None
    }

    assert scanned == set(EnumAutomationLivenessSourceClass)


def test_parity_with_internal_copy_planted_divergence_control_names_a_changed_recorded_cell(
    tmp_path: Path,
) -> None:
    case = _case("planted_undeclared_unit")
    node = _run_node(case, tmp_path)
    assert divergence(case["recorded"], node) == []

    exit_code = {**case["recorded"], "exit_code": 0}
    subject = {
        **case["recorded"],
        "stdout": [
            case["recorded"]["stdout"][0].replace("onex-unlisted", "onex-other"),
            *case["recorded"]["stdout"][1:],
        ],
    }
    dropped = {**case["recorded"], "stdout": case["recorded"]["stdout"][1:]}

    assert divergence(exit_code, node) == ["exit_code: recorded=0 node=1"]
    assert any(
        line.startswith("finding[0].subject:") for line in divergence(subject, node)
    )
    assert any(line.startswith("finding_count:") for line in divergence(dropped, node))


def test_parity_with_internal_copy_planted_divergence_control_names_a_changed_node_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case("several_findings_at_once")
    original: Callable[..., Any] = HandlerAutomationLivenessOverlayCheck.handle

    def drops_a_finding(
        self: HandlerAutomationLivenessOverlayCheck, request: Any
    ) -> Any:
        result = original(self, request)
        return result.model_copy(update={"findings": result.findings[1:]})

    monkeypatch.setattr(
        HandlerAutomationLivenessOverlayCheck, "handle", drops_a_finding
    )

    named = divergence(case["recorded"], _run_node(case, tmp_path))

    assert any(line.startswith("finding_count:") for line in named)
    assert any(line.startswith("summary:") for line in named)


def test_parity_with_internal_copy_declares_one_divergence_the_wildcard_rule(
    tmp_path: Path,
) -> None:
    case = _case(_GLOB_DIVERGENCE)
    recorded_findings = case["recorded"]["stdout"][:-1]

    node = _run_node(case, tmp_path)

    assert len(recorded_findings) == 1
    assert recorded_findings[0].startswith(
        "undeclared_source: omnibase_infra/plugins/prune_plugin_store.py:"
    )
    assert case["recorded"]["exit_code"] == 1
    assert node["exit_code"] == 0
    assert node["stdout"][:-1] == []
    assert divergence(case["recorded"], node) == [
        "exit_code: recorded=1 node=0",
        "finding[0].detail: recorded="
        + repr(recorded_findings[0].partition(": ")[2].partition(": ")[2])
        + " node=None",
        "finding[0].kind: recorded='undeclared_source' node=None",
        "finding[0].subject: recorded='omnibase_infra/plugins/prune_plugin_store.py' node=None",
        "finding_count: recorded=1 node=0",
        "summary: recorded=('1', '2', '2', '1', 'complete') node=('1', '1', '2', '0', 'complete')",
    ]


def test_planted_undeclared_unit_matches_the_recorded_internal_copy(
    tmp_path: Path,
) -> None:
    case = _case("planted_undeclared_unit")

    node = _run_node(case, tmp_path)

    assert case["recorded"]["exit_code"] == 1
    assert case["recorded"]["stdout"][:-1] == node["stdout"][:-1]
    assert "onex-unlisted.timer" in node["stdout"][0]


def test_planted_undeclared_unit_makes_the_node_alone_report_a_finding(
    tmp_path: Path,
) -> None:
    """Planted unit file: the node reports it; remove the unit and the same run is clean."""
    case = _case("planted_undeclared_unit")
    _materialize(case, tmp_path)
    request = ModelAutomationLivenessOverlayCheckRunRequest(
        internal_root=tmp_path / _PRIVATE, registry_root=tmp_path
    )

    refused = HandlerAutomationLivenessOverlayCheckRun().handle(request)
    (tmp_path / "omnibase_infra/deploy/units/onex-unlisted.timer").unlink()
    agreed = HandlerAutomationLivenessOverlayCheckRun().handle(request)

    assert refused.exit_code == 1
    assert refused.stdout_lines[0].startswith(
        "undeclared_source: omnibase_infra/deploy/units/onex-unlisted.timer:"
    )
    assert agreed.exit_code == 0
    assert agreed.stdout_lines[:-1] == ()
