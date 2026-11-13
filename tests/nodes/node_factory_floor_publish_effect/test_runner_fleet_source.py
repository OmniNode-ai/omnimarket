# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publisher reads the existing fleet producer before building the floor."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_runner_fleet,
    load_runner_fleet_strict,
    runner_fleet_cause,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS

pytestmark = pytest.mark.unit
OBSERVATION = {
    "observed_at": "2026-11-04T20:00:00+00:00",
    "observing_host": "monitor-host",
    "runners": [{"host": "worker-a", "status": "busy"}],
}


def test_fleet_source_is_refreshed_before_the_floor_from_the_declared_producer(
    tmp_path: Path,
) -> None:
    from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
        FACTORY,
        Scripted,
        _refresh_request,
    )

    overlay_root = tmp_path / "overlays"
    reads_overlay = overlay_root / "runtime_host_reads/overlay.yaml"
    reads_overlay.parent.mkdir(parents=True)
    reads_overlay.write_text(
        "ssh_host_env: LAB_SSH\ncompose_project: lab\nfleet_service: fleet-runtime\n"
        "provenance_service: build-runtime\nprovenance_path: /app/build-provenance.json\n"
        "repo_refs:\n  - {repo: lab_repo, clone_relpath: lab_repo, ref_path: [infra_vcs_ref]}\n"
    )
    request = _refresh_request(tmp_path)
    target = Path(request.home or "") / ".onex_state/runner-fleet/runner-fleet.json"
    reads = []

    class FleetSource(Scripted):
        def __call__(
            self, argv: Sequence[str], stdin: bytes | None, timeout: int
        ) -> subprocess.CompletedProcess[bytes]:
            if (
                argv[0] == "ssh"
                and argv[-1] == "bash -s"
                and stdin
                and b"@@FLEET" in stdin
            ):
                reads.append((list(argv), stdin))
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(OBSERVATION).encode(), b""
                )
            if FACTORY in argv:
                assert load_runner_fleet_strict(target).runners[0].status == "busy"
            return super().__call__(argv, stdin, timeout)

    result = HandlerFactoryFloorPublish(
        FleetSource({"--json": (0, b"[]", b"")})
    ).handle(
        request,
        environ={
            "CLAUDE_PLUGIN_ROOT": "/plugin-root",
            "ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root),
            "LAB_SSH": "lab-host",
        },
    )
    assert len(reads) == 1
    assert "lab-host" in reads[0][0]
    assert b"node_projection_runner_fleet" in reads[0][1]
    assert b"label=com.docker.compose.service=fleet-runtime" in reads[0][1]
    assert (
        next(step for step in result.steps if step.name == "runner_fleet").status
        == "ok"
    )


def test_fleet_read_failure_replaces_old_counts_with_an_unread_cause(
    tmp_path: Path,
) -> None:
    from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
        Scripted,
        _refresh_request,
    )

    request = _refresh_request(tmp_path)
    target = Path(request.home or "") / ".onex_state/runner-fleet/runner-fleet.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(OBSERVATION))
    result = HandlerFactoryFloorPublish(Scripted({"--json": (0, b"[]", b"")})).handle(
        request, environ={"CLAUDE_PLUGIN_ROOT": "/plugin-root"}
    )
    assert load_runner_fleet(target) is None
    assert "runtime_host_reads" in runner_fleet_cause(target)
    assert (
        next(step for step in result.steps if step.name == "runner_fleet").status
        == "failed"
    )


def test_fleet_refresh_is_concurrent_and_uses_the_floor_input_path() -> None:
    from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
        OWN_CONTRACT,
    )
    from omnimarket.nodes.node_factory_floor_effect.source_loader import load_contract

    steps = load_runtime().with_limits(TEST_LIMITS).steps
    source = next(step for step in steps if step.name == "runner_fleet")
    assert source.concurrent
    assert not source.required
    assert (
        source.output_relpath
        == load_contract(OWN_CONTRACT).runtime.sources.runner_fleet_relpath
    )
    assert list(steps).index(source) < next(
        i for i, step in enumerate(steps) if step.name == "factory_floor"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"{}",
        json.dumps({**OBSERVATION, "observed_at": "2026-11-04T20:00:00"}).encode(),
        json.dumps(
            {**OBSERVATION, "runners": [{"host": "worker-a", "status": "unknown"}]}
        ).encode(),
        b'{"status":"unread","reason":"producer has no observation"}',
    ],
)
def test_an_invalid_or_unread_producer_never_supplies_fleet_counts(
    payload: bytes,
) -> None:
    from omnimarket.models.model_runtime_host_reads import (
        ModelRuntimeHostReadOverlay,
        ModelRuntimeRepoRef,
    )
    from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_fleet_observation_source import (
        HandlerFleetObservationSource,
    )
    from omnimarket.nodes.node_factory_floor_publish_effect.models.model_fleet_observation_source import (
        ModelFleetObservationSourceRequest,
    )

    def run(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        assert timeout == 7
        return subprocess.CompletedProcess(argv, 0, payload, b"")

    overlay = ModelRuntimeHostReadOverlay(
        ssh_host_env="LAB_SSH",
        compose_project="lab",
        fleet_service="fleet-runtime",
        provenance_service="build-runtime",
        provenance_path="/app/build-provenance.json",
        repo_refs=(
            ModelRuntimeRepoRef(
                repo="lab_repo", clone_relpath="lab_repo", ref_path=("infra_vcs_ref",)
            ),
        ),
    )
    document = (
        HandlerFleetObservationSource(run)
        .handle(
            ModelFleetObservationSourceRequest(overlay=overlay, timeout_seconds=7),
            environ={"LAB_SSH": "lab-host"},
        )
        .document
    )
    assert document["status"] == "unread"
    assert document["reason"]
    assert "runners" not in document
