# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_duplication_sweep (OMN-13676).

COMPUTE node with self-collected I/O. PRE-WORK note: of the four checks, D1
(Drizzle tables), D2 (Kafka topic registration) and D4 (cross-repo model names)
are pure *filesystem* scans rooted at ``omni_home`` — so we inject synthetic
input by building a real, minimal ``omni_home`` tree under ``tmp_path`` and run
the actual ``NodeDuplicationSweep().handle()`` against it. This is genuine
input-injection (real files), not a subprocess/asyncpg monkeypatch.

Only D3 (migration-prefix collisions) shells out to ``check-migration-conflicts``
and cannot run hermetically in CI; it is excluded via the ``checks`` param (a
supported request axis) and tracked as the residual seam follow-up. Each case
asserts the typed per-check status + finding structure; the negative-control
cases (dup table / topic conflict / model collision / unresolvable scope) each
force a FAIL/ERROR finding.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_duplication_sweep.handlers.handler_duplication_sweep import (
    DuplicationSweepRequest,
    NodeDuplicationSweep,
)

# --- synthetic omni_home builders ----------------------------------------- #


def _mk(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_d1_clean(home: Path) -> None:
    _mk(
        home / "omnidash/shared/users-schema.ts",
        'export const users = pgTable("users", {})',
    )


def _build_d1_dup(home: Path) -> None:
    _mk(home / "omnidash/shared/a-schema.ts", 'pgTable("users", {})')
    _mk(home / "omnidash/shared/b-schema.ts", 'pgTable("users", {})')


def _topics_py(topic: str) -> str:
    return f'FOO_TOPIC = "{topic}"\n'


def _boundaries_yaml(topic: str, producer: str) -> str:
    return f'topics:\n  - topic_name: "{topic}"\n    producer_repo: "{producer}"\n'


def _build_d2_clean(home: Path) -> None:
    topic = "onex.evt.demo.thing.v1"
    _mk(home / "omniclaude/src/omniclaude/hooks/topics.py", _topics_py(topic))
    _mk(
        home
        / "onex_change_control/src/onex_change_control/boundaries/kafka_boundaries.yaml",
        _boundaries_yaml(topic, "omniclaude"),  # same producer → no conflict
    )


def _build_d2_conflict(home: Path) -> None:
    topic = "onex.evt.demo.thing.v1"
    _mk(home / "omniclaude/src/omniclaude/hooks/topics.py", _topics_py(topic))
    _mk(
        home
        / "onex_change_control/src/onex_change_control/boundaries/kafka_boundaries.yaml",
        _boundaries_yaml(topic, "omnibase_infra"),  # different producer → conflict
    )


def _build_d4_clean(home: Path) -> None:
    _mk(home / "repo_a/src/repo_a/m.py", "class ModelAlpha:\n    pass\n")
    _mk(home / "repo_b/src/repo_b/n.py", "class ModelBeta:\n    pass\n")
    (home / "repo_a/.git").mkdir(parents=True, exist_ok=True)
    (home / "repo_b/.git").mkdir(parents=True, exist_ok=True)


def _build_d4_collision(home: Path) -> None:
    _mk(home / "repo_a/src/repo_a/m.py", "class ModelDup:\n    pass\n")
    _mk(home / "repo_b/src/repo_b/n.py", "class ModelDup:\n    pass\n")
    (home / "repo_a/.git").mkdir(parents=True, exist_ok=True)
    (home / "repo_b/.git").mkdir(parents=True, exist_ok=True)


# (id, builder, checks, check_id, expected_check_status, expected_finding_name)
CASES = [
    pytest.param(
        _build_d1_clean, ["D1"], "D1", "PASS", None, id="d1-no-duplicate-tables"
    ),
    pytest.param(
        _build_d1_dup, ["D1"], "D1", "FAIL", "users", id="d1-duplicate-drizzle-table"
    ),
    pytest.param(
        _build_d2_clean, ["D2"], "D2", "PASS", None, id="d2-no-topic-conflict"
    ),
    pytest.param(
        _build_d2_conflict,
        ["D2"],
        "D2",
        "FAIL",
        "onex.evt.demo.thing.v1",
        id="d2-topic-registration-conflict",
    ),
    pytest.param(
        _build_d4_clean, ["D4"], "D4", "PASS", None, id="d4-no-model-collision"
    ),
    pytest.param(
        _build_d4_collision,
        ["D4"],
        "D4",
        "FAIL",
        "ModelDup",
        id="d4-cross-repo-model-collision",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("builder", "checks", "check_id", "expected_status", "expected_finding_name"),
    [(c.values[0], c.values[1], c.values[2], c.values[3], c.values[4]) for c in CASES],
    ids=[c.id for c in CASES],
)
def test_duplication_sweep_multiparam(
    tmp_path: Path,
    builder: Callable[[Path], None],
    checks: list[str],
    check_id: str,
    expected_status: str,
    expected_finding_name: str | None,
) -> None:
    home = tmp_path / "omni_home"
    home.mkdir()
    builder(home)

    result = NodeDuplicationSweep().handle(
        DuplicationSweepRequest(omni_home=str(home), checks=checks)
    )

    by_id = {r.check_id: r for r in result.check_results}
    assert check_id in by_id, f"missing check result for {check_id}"
    check = by_id[check_id]
    assert check.status == expected_status

    if expected_status == "FAIL":
        assert result.overall_status == "FAIL"
        assert check.finding_count >= 1
        names = {f.name for f in check.findings}
        assert expected_finding_name in names, (
            f"expected finding {expected_finding_name!r}, got {sorted(names)}"
        )
    else:
        assert check.findings == []
        assert result.overall_status == "PASS"


@pytest.mark.integration
def test_duplication_sweep_unresolvable_scope_errors(tmp_path: Path) -> None:
    """Negative control: an omni_home that is not a directory must fail loud
    (overall ERROR), never report a false-clean PASS over an unresolvable scope."""
    missing = tmp_path / "does_not_exist"
    result = NodeDuplicationSweep().handle(
        DuplicationSweepRequest(omni_home=str(missing), checks=["D1"])
    )
    assert result.overall_status == "ERROR"
    assert result.check_results[0].check_id == "scope"
    assert result.check_results[0].status == "FAIL"
