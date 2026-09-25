# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared local capacity is the capacity the model server serves (OMN-19447).

``saturation_policy.tiers[local].max_concurrent_generations`` said 1 long after
the .201 server began running four sequences at once and the delegate-skill
orchestrator began admitting four records (OMN-18852). The nightly paces its
corpus from that number, so a stale 1 made it run one case at a time behind a
server that could take four.

The declared number is now pinned to the server argument recorded beside this
file. Changing either without the other turns this red.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.delegation_golden import runner as runner_module

_RECORD = Path(__file__).with_name("local_serving_capacity_omn19447.yaml")


def _record() -> dict[str, Any]:
    raw = yaml.safe_load(_RECORD.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _declared_local_rule() -> dict[str, Any]:
    raw = yaml.safe_load(
        runner_module._BIFROST_DELEGATION_CONFIG.read_text(encoding="utf-8")
    )
    return next(
        rule for rule in raw["saturation_policy"]["tiers"] if rule["tier"] == "local"
    )


@pytest.mark.unit
class TestDeclaredLocalCapacityMatchesTheServer:
    def test_record_names_the_local_tier(self) -> None:
        assert _record()["tier"] == "local"

    def test_declared_capacity_equals_recorded_max_num_seqs(self) -> None:
        recorded = _record()["server"]["max_num_seqs"]
        assert isinstance(recorded, int)
        assert recorded >= 1
        assert _declared_local_rule()["max_concurrent_generations"] == recorded

    def test_the_nightly_paces_at_the_recorded_capacity(self) -> None:
        """AC3's precondition: the probe's wave width is the served capacity."""
        recorded = _record()["server"]["max_num_seqs"]
        assert runner_module.lane_serving_concurrency() == recorded

    def test_the_measured_peak_never_exceeded_the_declared_capacity(self) -> None:
        """AC1's record: at 4 and 8 concurrent the server ran at most 4."""
        declared = _declared_local_rule()["max_concurrent_generations"]
        measured = _record()["measurements"]
        assert {m["concurrent"] for m in measured} == {4, 8}
        for m in measured:
            assert m["completed"] == m["concurrent"]
            assert m["server_peak_running"] == declared

    def test_a_drifted_declaration_is_caught(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: a declaration that disagrees with the record fails."""
        raw = yaml.safe_load(
            runner_module._BIFROST_DELEGATION_CONFIG.read_text(encoding="utf-8")
        )
        recorded = _record()["server"]["max_num_seqs"]
        for rule in raw["saturation_policy"]["tiers"]:
            if rule["tier"] == "local":
                rule["max_concurrent_generations"] = recorded + 1
        drifted = tmp_path / "bifrost_delegation.yaml"
        drifted.write_text(yaml.safe_dump(raw), encoding="utf-8")
        monkeypatch.setattr(runner_module, "_BIFROST_DELEGATION_CONFIG", drifted)
        assert runner_module.lane_serving_concurrency() != recorded
