# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19013: the Core floor admits only a release carrying the construction pair.

omnibase-core 0.47.22 is the first published release carrying core#1737: the
``TERMINAL_CONSTRUCTION_FAILED`` operational outcome and the ``UNDETERMINED``
content verdict. Migration 0045 in this repo filters on exactly that pair, and
omnibase_infra vendors 0045 from here, so a resolve below the floor would ship
a projection filter for values no installed terminal model can carry.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[3]
_PAIR_RELEASE = Version("0.47.22")


def _core_requirements() -> list[Requirement]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = [
        *data["project"]["dependencies"],
        *data["tool"]["uv"]["override-dependencies"],
    ]
    return [
        Requirement(spec)
        for spec in declared
        if Requirement(spec).name == "omnibase-core"
    ]


@pytest.mark.unit
def test_core_floor_excludes_every_release_without_the_pair() -> None:
    requirements = _core_requirements()
    # Both places, in lockstep: the override decides transitive resolves.
    assert len(requirements) == 2
    for requirement in requirements:
        assert not requirement.specifier.contains("0.47.21"), str(requirement)
        # The floor may rise past the pair release (OMN-19531 raised it to
        # 0.47.23); what must hold is that it never drops below it.
        floors = [
            Version(spec.version)
            for spec in requirement.specifier
            if spec.operator == ">="
        ]
        assert floors, str(requirement)
        assert min(floors) >= _PAIR_RELEASE, str(requirement)


@pytest.mark.unit
def test_installed_core_carries_the_construction_pair() -> None:
    from omnibase_core.enums.enum_delegation_content_verdict import (
        EnumDelegationContentVerdict,
    )
    from omnibase_core.enums.enum_delegation_operational_outcome import (
        EnumDelegationOperationalOutcome,
    )

    assert (
        EnumDelegationOperationalOutcome.TERMINAL_CONSTRUCTION_FAILED.value
        == "terminal_construction_failed"
    )
    assert EnumDelegationContentVerdict.UNDETERMINED.value == "undetermined"


@pytest.mark.unit
def test_migration_0045_filters_on_the_core_pair_values() -> None:
    sql = (
        _ROOT
        / "src/omnimarket/nodes/node_projection_delegation/migrations"
        / "0045_terminal_construction_outcome_metrics.sql"
    ).read_text(encoding="utf-8")
    assert "('terminal_construction_failed', 'undetermined')" in sql


@pytest.mark.unit
def test_migration_0045_reasserts_security_invoker_on_every_view_it_replaces() -> None:
    """CREATE OR REPLACE VIEW drops reloptions; 0045 must put them back."""
    sql = (
        _ROOT
        / "src/omnimarket/nodes/node_projection_delegation/migrations"
        / "0045_terminal_construction_outcome_metrics.sql"
    ).read_text(encoding="utf-8")
    replaced = re.findall(r"CREATE OR REPLACE VIEW (\w+) AS", sql)
    assert sorted(replaced) == [
        "projection_delegation_model_routing",
        "projection_delegation_quality_gate",
        "projection_delegation_summary",
    ]
    for view in replaced:
        assert f"ALTER VIEW {view} SET (security_invoker = true);" in sql, view


def _failed_terminal(**overrides: Any) -> Any:
    from omnibase_core.enums.enum_delegation_content_verdict import (
        EnumDelegationContentVerdict,
    )
    from omnibase_core.enums.enum_delegation_operational_outcome import (
        EnumDelegationOperationalOutcome,
    )
    from omnibase_core.models.delegation.wire.model_delegation_failed import (
        ModelDelegationFailed,
    )

    fields: dict[str, Any] = {
        "correlation_id": uuid4(),
        "task_type": "test",
        "model_used": "m",
        "endpoint_url": "http://endpoint.invalid/v1",
        "content": "the answer",
        "quality_passed": True,
        "operational_outcome": (
            EnumDelegationOperationalOutcome.TERMINAL_CONSTRUCTION_FAILED
        ),
        "content_verdict": EnumDelegationContentVerdict.UNDETERMINED,
        "quality_score": None,
        "latency_ms": 1,
        "fallback_to_claude": False,
        "failure_reason": "terminal_construction_failed: x",
        "terminal_failure_reason": "terminal_construction_failed",
    }
    fields.update(overrides)
    return ModelDelegationFailed(**fields)


@pytest.mark.unit
def test_core_failed_topic_keeps_the_gate_flag_only_for_the_construction_pair() -> None:
    """The failed topic states delivery failed; it does not rewrite the gate fact."""
    from omnibase_core.enums.enum_delegation_content_verdict import (
        EnumDelegationContentVerdict,
    )
    from omnibase_core.enums.enum_delegation_operational_outcome import (
        EnumDelegationOperationalOutcome,
    )

    kept = _failed_terminal()
    assert kept.quality_passed is True

    with pytest.raises(ValueError, match="quality_passed"):
        _failed_terminal(
            operational_outcome=EnumDelegationOperationalOutcome.INFERENCE_FAILED,
            content_verdict=EnumDelegationContentVerdict.USABLE,
        )


@pytest.mark.unit
def test_core_construction_terminal_survives_a_canonical_json_round_trip() -> None:
    from omnibase_core.models.delegation.wire.model_delegation_failed import (
        ModelDelegationFailed,
    )

    for passed in (True, False):
        terminal = _failed_terminal(quality_passed=passed)
        restored = ModelDelegationFailed.model_validate_json(terminal.model_dump_json())
        assert restored == terminal
        assert restored.quality_passed is passed
        assert restored.quality_score is None


@pytest.mark.unit
def test_installed_core_pair_is_the_pair_the_market_receiver_accepts() -> None:
    """The Market receiver takes the pair Core reserves, field for field.

    The terminal is built by the installed Core release and its pair is handed to
    the Market response model unchanged. A receiver that does not declare the pair
    refuses the fields, so this fails wherever the Market side is missing.
    """
    from omnimarket.models.delegation.wire.model_delegate_skill_response import (
        ModelDelegateSkillResponse,
    )

    core = _failed_terminal()
    received = ModelDelegateSkillResponse.model_validate(
        {
            "status": "failed",
            "correlation_id": core.correlation_id,
            "task_type": core.task_type,
            "operational_outcome": core.operational_outcome.value,
            "content_verdict": core.content_verdict.value,
            "terminal_failure_reason": core.terminal_failure_reason,
            "quality_gate_passed": core.quality_passed,
        }
    )
    assert received.operational_outcome is core.operational_outcome
    assert received.content_verdict is core.content_verdict
    assert received.terminal_failure_reason == core.terminal_failure_reason
    assert received.quality_gate_passed is core.quality_passed
    assert received.quality_score is None


@pytest.mark.unit
def test_migration_0045_pair_is_the_pair_the_projection_writer_leaves_unscored() -> (
    None
):
    """The pair the migration filters on is the pair the writer stores without a score."""
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        _canonical_result_to_task_delegated_payload,
    )

    sql = (
        _ROOT
        / "src/omnimarket/nodes/node_projection_delegation/migrations"
        / "0045_terminal_construction_outcome_metrics.sql"
    ).read_text(encoding="utf-8")
    match = re.search(r"\('(\w+)', '(\w+)'\)", sql)
    assert match is not None
    outcome, verdict = match.groups()

    row = _canonical_result_to_task_delegated_payload(
        {
            "correlation_id": str(uuid4()),
            "quality_passed": True,
            "operational_outcome": outcome,
            "content_verdict": verdict,
            "quality_score": None,
            "actual_score": 0.9,
        }
    )
    assert row["operational_outcome"] == outcome
    assert row["content_verdict"] == verdict
    assert row["actual_score"] is None
