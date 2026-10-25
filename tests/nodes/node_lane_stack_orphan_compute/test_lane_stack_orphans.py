# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which lane-started containers outlive their lane (OMN-21288, lane h201-swap-0625).

On 2026-10-25 four lane postgres containers (``landing-l15919-pg``, ``omn19448-projection-test-db``,
``rl1215-pg``, ``landing-l15299-pg``) were still running 32 to 46 hours after their lanes ended, on
a shared lab host. Neither ``onex-lane-closeout`` (worktrees) nor the lane reaper (processes, and
it never touches docker) removed them. The node judges a host's container inventory against the
lanes that are live and the lanes that are finished.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit

PACKAGE = "omnimarket.nodes.node_lane_stack_orphan_compute"
MODELS = PACKAGE + ".models.model_lane_stack_orphan"
HANDLER = PACKAGE + ".handlers.handler_lane_stack_orphan"


def _models():
    return importlib.import_module(MODELS)


def _judge(
    containers: list[dict[str, object]],
    *,
    live: tuple[str, ...] = (),
    finished: tuple[str, ...] = (),
):
    models = _models()
    handler = importlib.import_module(HANDLER).HandlerLaneStackOrphan()
    request = models.ModelLaneStackOrphanRequest(
        containers=tuple(models.ModelLaneStackContainer(**c) for c in containers),
        live_lanes=live,
        finished_lanes=finished,
    )
    return handler.handle(request)


def _by_name(verdict) -> dict[str, object]:
    return {f.name: f for f in verdict.findings}


def test_finished_lane_container_is_stopped_with_restore_command() -> None:
    verdict = _judge(
        [
            {
                "name": "rl1215-pg",
                "state": "running",
                "lane": "pool-capacity-0215",
                "mem_bytes": 7 * 1024 * 1024,
            }
        ],
        finished=("pool-capacity-0215",),
    )
    finding = _by_name(verdict)["rl1215-pg"]
    assert finding.verdict == "stop"
    assert finding.stop_command == "docker stop rl1215-pg"
    assert finding.restore_command == "docker start rl1215-pg"
    assert verdict.stop_names == ("rl1215-pg",)
    assert verdict.mem_bytes_freed == 7 * 1024 * 1024


def test_live_lane_container_is_kept_even_when_the_lane_also_reads_finished() -> None:
    verdict = _judge(
        [{"name": "rl1215-pg", "state": "running", "lane": "pool-capacity-0215"}],
        live=("pool-capacity-0215",),
        finished=("pool-capacity-0215",),
    )
    finding = _by_name(verdict)["rl1215-pg"]
    assert finding.verdict == "keep_live_lane"
    assert finding.stop_command is None
    assert verdict.stop_names == ()


@pytest.mark.parametrize(
    "name",
    [
        "omninode-runtime",
        "omnibase-infra-postgres",
        "omnibase-infra-redpanda",
        "omninode-runner-4",
        "omninode-verify-runner-1",
        "omninode-prod-deploy-runner-1",
        "omninode-customer-plane-runner-2",
        "omninode-gate-runner",
        "omninode-pypi-cache",
        "omnimarket-projection-api",
        "onex-api",
        "dockerhub-mirror",
    ],
)
def test_a_lane_label_cannot_claim_a_protected_container(name: str) -> None:
    verdict = _judge(
        [{"name": name, "state": "running", "lane": "pool-capacity-0215"}],
        finished=("pool-capacity-0215",),
    )
    assert _by_name(verdict)[name].verdict == "keep_protected"
    assert verdict.stop_names == ()


def test_unlabelled_container_is_reported_unowned_and_never_stopped() -> None:
    verdict = _judge(
        [{"name": "landing-l15299-pg", "state": "running"}],
        finished=("pool-capacity-0215",),
    )
    finding = _by_name(verdict)["landing-l15299-pg"]
    assert finding.verdict == "unowned"
    assert finding.stop_command is None
    assert verdict.stop_names == ()
    assert verdict.unowned_names == ("landing-l15299-pg",)


def test_unknown_lane_label_is_kept() -> None:
    verdict = _judge([{"name": "x-pg", "state": "running", "lane": "ghost-lane"}])
    assert _by_name(verdict)["x-pg"].verdict == "keep_unknown_lane"
    assert verdict.stop_names == ()


def test_exited_container_of_a_finished_lane_needs_no_stop() -> None:
    verdict = _judge(
        [{"name": "x-pg", "state": "exited", "lane": "done-lane"}],
        finished=("done-lane",),
    )
    finding = _by_name(verdict)["x-pg"]
    assert finding.verdict == "already_stopped"
    assert finding.stop_command is None
    assert verdict.stop_names == ()


def test_verdict_keeps_inventory_order_and_sums_only_stopped_memory() -> None:
    verdict = _judge(
        [
            {"name": "a-pg", "state": "running", "lane": "done", "mem_bytes": 10},
            {"name": "b-pg", "state": "running", "lane": "busy", "mem_bytes": 100},
            {"name": "c-pg", "state": "running", "lane": "done", "mem_bytes": 5},
        ],
        live=("busy",),
        finished=("done",),
    )
    assert [f.name for f in verdict.findings] == ["a-pg", "b-pg", "c-pg"]
    assert verdict.stop_names == ("a-pg", "c-pg")
    assert verdict.mem_bytes_freed == 15


def test_contract_golden_chain() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == "node_lane_stack_orphan_compute"
    assert contract["metadata"]["related_tickets"] == ["OMN-21288"]
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert metadata["name"] == "node_lane_stack_orphan_compute"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
