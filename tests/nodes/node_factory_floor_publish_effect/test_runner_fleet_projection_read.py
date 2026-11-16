# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner fleet is read from the bus-backed projection first; one ssh into a loaded host is no longer the only way."""

import json
import subprocess
from collections.abc import Sequence

import pytest

from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_fleet_observation_source import (
    HandlerFleetObservationSource,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_fleet_observation_source import (
    ModelFleetObservationSourceRequest,
)

pytestmark = pytest.mark.unit

URL = "http://runtime-host:8086/projection-api/projection/onex.snapshot.projection.runner-fleet.v1"
BOUND = 10
ROW = {
    "projection_cursor": 6,
    "runner_name": "lab-runner-1",
    "label_class": "omnibase-ci",
    "labels": ["self-hosted", "arch-amd64"],
    "host": "lab-host-a",
    "status": "online",
}
PAGE = {
    "latest_event_at": "2026-11-09T15:27:12.328291+00:00",
    "row_count": 1,
    "truncated": False,
    "next_cursor": None,
    "rows": [ROW],
}
OBSERVATION = {
    "observed_at": "2026-11-09T15:27:00+00:00",
    "runners": [{"host": "lab-host-b", "status": "busy", "runner_name": "r2"}],
}


def _overlay(url: str | None) -> ModelRuntimeHostReadOverlay:
    return ModelRuntimeHostReadOverlay.model_validate(
        {
            "ssh_host_env": "LAB_SSH",
            "compose_project": "lab",
            "fleet_service": "fleet-runtime",
            "provenance_service": "build-runtime",
            "provenance_path": "/app/build-provenance.json",
            "repo_refs": [
                {
                    "repo": "lab_repo",
                    "clone_relpath": "lab_repo",
                    "ref_path": ["infra_vcs_ref"],
                }
            ],
            "fleet_projection_url": url,
        }
    )


class Ssh:
    """The ssh boundary: counts calls, records the bound, and replays a timed-out ssh or answers with the saved file."""

    def __init__(self, timed_out: bool) -> None:
        self.timed_out = timed_out
        self.timeouts: list[int] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> "subprocess.CompletedProcess[bytes]":
        self.timeouts.append(timeout)
        if self.timed_out:
            raise PublishRefusedError(f"{argv[0]} did not finish within {timeout} s")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(OBSERVATION).encode(), b""
        )


def _fetch_of(page: object, seen: list[tuple[str, int]] | None = None):
    def fetch(url: str, timeout: int) -> bytes:
        if seen is not None:
            seen.append((url, timeout))
        return json.dumps(page).encode()

    return fetch


def _read(url: str | None, ssh: Ssh, fetch=None):
    handler = HandlerFleetObservationSource(ssh, fetch or _fetch_of(PAGE))
    return handler.handle(
        ModelFleetObservationSourceRequest(
            overlay=_overlay(url), timeout_seconds=BOUND
        ),
        environ={"LAB_SSH": "lab-host"},
    ).document


def test_failing_control_a_timed_out_ssh_alone_draws_the_fleet_unread() -> None:
    # No projection url: the one ssh is the only read, and its timeout is the whole reading (the red the floor row showed).
    ssh = Ssh(timed_out=True)
    document = _read(None, ssh)
    assert document["status"] == "unread"
    assert document["reason"] == "ssh did not finish within 10 s"
    assert ssh.timeouts == [BOUND]


def test_golden_the_projection_answers_and_no_ssh_is_made() -> None:
    ssh = Ssh(timed_out=True)
    seen: list[tuple[str, int]] = []
    document = _read(URL, ssh, _fetch_of(PAGE, seen))
    assert document["status"] == "read"
    assert document["observed_at"] == "2026-11-09T15:27:12.328291+00:00"
    assert document["runners"] == [
        {
            "host": "lab-host-a",
            "status": "online",
            "runner_name": "lab-runner-1",
            "label_class": "omnibase-ci",
            "labels": ["self-hosted", "arch-amd64"],
        }
    ]
    assert ssh.timeouts == []
    assert seen == [(URL, 5)]


def test_a_failed_projection_falls_back_to_ssh_inside_the_same_bound() -> None:
    ssh = Ssh(timed_out=False)

    def down(url: str, timeout: int) -> bytes:
        raise OSError("connection refused")

    document = _read(URL, ssh, down)
    assert document["status"] == "read"
    assert document["runners"][0]["host"] == "lab-host-b"
    assert len(ssh.timeouts) == 1
    assert 1 <= ssh.timeouts[0] <= BOUND


@pytest.mark.parametrize(
    ("page", "cause"),
    [
        ({**PAGE, "truncated": True}, "partial"),
        ({**PAGE, "next_cursor": "abc"}, "partial"),
        ({**PAGE, "row_count": 2}, "row count"),
        ({**PAGE, "rows": [], "row_count": 0}, "no runner rows"),
        ({k: v for k, v in PAGE.items() if k != "latest_event_at"}, "latest_event_at"),
        ({**PAGE, "rows": [{**ROW, "status": "gone"}]}, ""),
        ([], "not an object"),
    ],
)
def test_error_chain_a_cut_or_malformed_page_and_a_timed_out_ssh_name_both_causes(
    page: object, cause: str
) -> None:
    ssh = Ssh(timed_out=True)
    document = _read(URL, ssh, _fetch_of(page))
    assert document["status"] == "unread"
    assert cause in document["reason"]
    assert (
        "fleet projection read failed" in document["reason"]
        or "validation" in document["reason"]
    )
    assert "ssh did not finish within" in document["reason"]


def test_a_cut_page_is_never_read_as_a_smaller_healthy_fleet() -> None:
    ssh = Ssh(timed_out=False)
    document = _read(URL, ssh, _fetch_of({**PAGE, "truncated": True}))
    # The cut page is refused and the ssh answer is used, never the cut page's rows.
    assert document["runners"][0]["host"] == "lab-host-b"


def test_the_projection_timeout_never_exceeds_the_step_bound() -> None:
    seen: list[tuple[str, int]] = []
    handler = HandlerFleetObservationSource(Ssh(False), _fetch_of(PAGE, seen))
    handler.handle(
        ModelFleetObservationSourceRequest(overlay=_overlay(URL), timeout_seconds=3),
        environ={"LAB_SSH": "lab-host"},
    )
    assert seen == [(URL, 3)]


def test_overlay_url_must_be_http() -> None:
    with pytest.raises(ValueError, match="fleet_projection_url"):
        _overlay("ssh://runtime-host")
