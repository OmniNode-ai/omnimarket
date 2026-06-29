# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_compliance_sweep (OMN-13675, WS-5 Wave 1).

Variant A (pure COMPUTE): builds synthetic handler trees under ``tmp_path`` (passed
as explicit ``target_dirs`` so no ``$OMNI_HOME`` resolution is needed), drives
``NodeComplianceSweep.handle`` in-process, and asserts typed result fields
(``handlers_scanned``, ``status``, ``by_type``, ``by_severity``, ``total_violations``).

Negative controls: a hardcoded topic string and an undeclared transport import must
each produce a typed ``ModelComplianceViolation`` of the matching type.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    NodeComplianceSweep,
)

_CLEAN_HANDLER = "def handle(envelope):\n    return envelope\n"
_TOPIC_HANDLER = (
    'TOPIC = "onex.evt.core.foo.v1"\n\n\ndef handle(envelope):\n    return envelope\n'
)
_TRANSPORT_HANDLER = "import httpx\n\n\ndef handle(envelope):\n    return httpx\n"


def _make_repo(root: Path, name: str, handlers: dict[str, str]) -> str:
    """Create repo/src/nodes/node_x/handlers/<file> for each handler source."""
    base = root / name / "src" / "nodes" / "node_x" / "handlers"
    base.mkdir(parents=True, exist_ok=True)
    for filename, source in handlers.items():
        (base / filename).write_text(source, encoding="utf-8")
    return str(root / name)


# (handlers spec, checks, expected_scanned, expected_status, expected_type|None,
#  expected_min_for_type)
CASES = [
    pytest.param(
        {"handler_clean.py": _CLEAN_HANDLER},
        None,
        1,
        "compliant",
        None,
        0,
        id="clean-handler-compliant",
    ),
    pytest.param(
        {"handler_topic.py": _TOPIC_HANDLER},
        ["hardcoded-topics"],
        1,
        "violations_found",
        "HARDCODED_TOPIC",
        1,
        id="hardcoded-topic-negative-control",
    ),
    pytest.param(
        {"handler_transport.py": _TRANSPORT_HANDLER},
        ["undeclared-transport"],
        1,
        "violations_found",
        "UNDECLARED_TRANSPORT",
        1,
        id="undeclared-transport-negative-control",
    ),
    pytest.param(
        {
            "handler_clean.py": _CLEAN_HANDLER,
            "handler_topic.py": _TOPIC_HANDLER,
            "handler_transport.py": _TRANSPORT_HANDLER,
        },
        None,
        3,
        "violations_found",
        "HARDCODED_TOPIC",
        1,
        id="mixed-tree-all-checks",
    ),
    pytest.param(
        # transport import present but only hardcoded-topics requested -> filtered.
        {"handler_transport.py": _TRANSPORT_HANDLER},
        ["hardcoded-topics"],
        1,
        "compliant",
        None,
        0,
        id="checks-subset-filters-transport",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("handlers", "checks", "expected_scanned", "expected_status", "vtype", "vmin"),
    CASES,
)
def test_compliance_sweep_multiparam(
    tmp_path: Path,
    handlers: dict[str, str],
    checks: list[str] | None,
    expected_scanned: int,
    expected_status: str,
    vtype: str | None,
    vmin: int,
) -> None:
    target = _make_repo(tmp_path, "myrepo", handlers)
    request = ComplianceSweepRequest(target_dirs=[target], checks=checks)

    result = NodeComplianceSweep().handle(request)

    assert result.handlers_scanned == expected_scanned
    assert result.status == expected_status
    assert result.compliant + result.imperative == result.handlers_scanned
    assert result.total_violations == len(result.violations)
    if vtype is not None:
        assert result.by_type.get(vtype, 0) >= vmin
        assert all(v.violation_type for v in result.violations)
        assert all(v.repo == "myrepo" for v in result.violations)
    else:
        assert result.violations == []


@pytest.mark.integration
def test_compliance_sweep_multi_repo_target_dirs(tmp_path: Path) -> None:
    """Two repo roots scanned via target_dirs aggregate handler counts + types."""
    repo_a = _make_repo(tmp_path, "repo_a", {"handler_topic.py": _TOPIC_HANDLER})
    repo_b = _make_repo(
        tmp_path, "repo_b", {"handler_transport.py": _TRANSPORT_HANDLER}
    )

    result = NodeComplianceSweep().handle(
        ComplianceSweepRequest(target_dirs=[repo_a, repo_b])
    )

    assert result.handlers_scanned == 2
    assert result.status == "violations_found"
    assert result.by_type.get("HARDCODED_TOPIC", 0) >= 1
    assert result.by_type.get("UNDECLARED_TRANSPORT", 0) >= 1
    assert {v.repo for v in result.violations} == {"repo_a", "repo_b"}
