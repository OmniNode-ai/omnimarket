# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor survives one serving host being down (OMN-23081).

The publish writes each file to a local copy first and then to every serving host the overlay names, each host on its
own. Golden: both hosts up, both served and the local copy complete. Error: a host down (ssh exit 255) or refusing a
write leaves the other host and the local copy updated and is named in the result and the served status; only a run
where no host stored a file refuses.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_operator_access import (
    HandlerOperatorAccess,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_reports_publish import (
    HandlerReportsPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelOperatorAccessRequest,
    ModelPublishTarget,
    ModelReportsPublishRequest,
)

pytestmark = pytest.mark.unit

RECORD = {
    "schema_id": "factory-floor/1",
    "generated_at": "2026-11-08T12:00:00+00:00",
    "lines": [],
    "floor": {"bays": [], "shipped": [], "waiting": [], "stoppages": []},
    "sources": {},
}
HOST_A = "host-a"
HOST_B = "host-b"


class TwoHosts:
    """Stands in for ssh to two serving hosts: each runs the remote command locally; a host in ``down`` never answers."""

    def __init__(
        self, down: Sequence[str] = (), refuse_writes: Sequence[str] = ()
    ) -> None:
        self.down = set(down)
        self.refuse_writes = set(refuse_writes)
        self.attempts: dict[str, int] = {}

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] != "ssh":
            return subprocess.CompletedProcess(list(argv), 0, b"", b"")
        host, remote = argv[-2], argv[-1]
        self.attempts[host] = self.attempts.get(host, 0) + 1
        if host in self.down:
            return subprocess.CompletedProcess(
                list(argv), 255, b"", b"ssh: connect to host timed out"
            )
        if host in self.refuse_writes and "cat >" in remote:
            return subprocess.CompletedProcess(list(argv), 1, b"", b"disk full")
        return subprocess.run(
            ["sh", "-c", remote], input=stdin, capture_output=True, check=False
        )


def _overlay(tmp_path: Path, *, local: bool = True) -> ModelFactoryFloorPublishOverlay:
    return ModelFactoryFloorPublishOverlay(
        ssh_target=HOST_A,
        remote_dir=str(tmp_path / "a/site"),
        serve_url="http://a.example.invalid:8088/",
        bus_lane="lane-x",
        history_root=str(tmp_path / "a/site"),
        owner_login="owner@example.invalid",
        extra_targets=(
            ModelPublishTarget(
                ssh_target=HOST_B,
                remote_dir=str(tmp_path / "b/site"),
                serve_url="http://b.example.invalid:8089/",
                history_root=str(tmp_path / "b/site"),
            ),
        ),
        local_copy_relpath="floor-copy" if local else None,
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    target = home / load_runtime().source.relpath
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(RECORD), encoding="utf-8")
    return home


def _publish(tmp_path: Path, ssh: TwoHosts, *, local: bool = True):  # type: ignore[no-untyped-def]
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)),
        overlay=_overlay(tmp_path, local=local),
        skip_refresh=True,
    )
    return HandlerFactoryFloorPublish(ssh).handle(request, environ={})


def _record_names(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# Golden chain: both hosts up.
def test_golden_both_hosts_and_the_local_copy_hold_every_file(tmp_path: Path) -> None:
    result = _publish(tmp_path, TwoHosts())

    assert result.served_to == (HOST_A, HOST_B)
    assert result.failed_targets == {}
    assert result.serve_urls == (
        "http://a.example.invalid:8088/",
        "http://b.example.invalid:8089/",
    )
    local = tmp_path / "home/floor-copy"
    for site in (tmp_path / "a/site", tmp_path / "b/site"):
        assert _record_names(site) == _record_names(local)
        assert json.loads((site / "factory-floor.json").read_text()) == RECORD
        status = json.loads((site / "publish-status.json").read_text())
        assert status["serve_urls"] == list(result.serve_urls)
        assert status["served_to"] == [HOST_A, HOST_B]
    assert json.loads((local / "factory-floor.json").read_text()) == RECORD


# Error chain: one host unreachable.
@pytest.mark.parametrize(("down", "up_site"), [(HOST_A, "b/site"), (HOST_B, "a/site")])
def test_error_one_host_down_still_updates_the_other_and_the_local_copy(
    tmp_path: Path, down: str, up_site: str
) -> None:
    ssh = TwoHosts(down=[down])

    result = _publish(tmp_path, ssh)

    up = HOST_B if down == HOST_A else HOST_A
    assert result.served_to == (up,)
    assert set(result.failed_targets) == {down}
    site = tmp_path / up_site
    assert json.loads((site / "factory-floor.json").read_text()) == RECORD
    assert (site / "index.html").is_file()
    local = tmp_path / "home/floor-copy"
    assert _record_names(site) == _record_names(local)
    status = json.loads((site / "publish-status.json").read_text())
    assert status["failed_targets"].keys() == {down}
    # The down host costs one attempt for the whole run, not one per file.
    assert ssh.attempts[down] == 1


def test_error_a_host_refusing_writes_does_not_stop_the_other(tmp_path: Path) -> None:
    result = _publish(tmp_path, TwoHosts(refuse_writes=[HOST_A]))

    assert result.served_to == (HOST_B,)
    assert set(result.failed_targets) == {HOST_A}
    assert "disk full" in result.failed_targets[HOST_A]
    assert (tmp_path / "b/site/factory-floor.json").is_file()
    assert not (tmp_path / "a/site/factory-floor.json").exists()


def test_error_every_host_down_refuses_but_the_local_copy_is_kept(
    tmp_path: Path,
) -> None:
    with pytest.raises(PublishRefusedError, match=HOST_A):
        _publish(tmp_path, TwoHosts(down=[HOST_A, HOST_B]))

    local = tmp_path / "home/floor-copy"
    assert json.loads((local / "factory-floor.json").read_text()) == RECORD
    assert not (tmp_path / "a/site").exists()
    assert not (tmp_path / "b/site").exists()


def test_error_the_failing_control_a_single_host_overlay_still_refuses_when_down(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path).model_copy(update={"extra_targets": ()})
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)), overlay=overlay, skip_refresh=True
    )
    with pytest.raises(PublishRefusedError, match="publishing"):
        HandlerFactoryFloorPublish(
            TwoHosts(down=[HOST_A]),
        ).handle(request, environ={})


def test_the_local_copy_is_optional_and_off_when_the_overlay_names_none(
    tmp_path: Path,
) -> None:
    _publish(tmp_path, TwoHosts(), local=False)

    assert not (tmp_path / "home/floor-copy").exists()


def test_the_history_snapshot_is_kept_on_each_host_and_locally(tmp_path: Path) -> None:
    result = _publish(tmp_path, TwoHosts())

    for root in (
        tmp_path / "a/site",
        tmp_path / "b/site",
        tmp_path / "home/floor-copy",
    ):
        assert (root / result.history_snapshot).is_file()


def test_reports_site_one_host_down_publishes_to_the_other_and_locally(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    result = HandlerReportsPublish(TwoHosts(down=[HOST_A])).handle(
        ModelReportsPublishRequest(home=str(home), overlay=_overlay(tmp_path)),
        environ={},
    )

    assert (tmp_path / "b/site/reports/manifest.json").is_file()
    assert not (tmp_path / "a/site").exists()
    assert (home / "floor-copy/reports/manifest.json").is_file()
    assert "reports/index.html" in result.published


def test_operator_access_one_host_down_gates_the_other(tmp_path: Path) -> None:
    from omnimarket.nodes.node_factory_floor_publish_effect.tests.test_operator_access import (
        MARKER,
        STATUS,
    )

    class Gate(TwoHosts):
        def __call__(self, argv, stdin, timeout):  # type: ignore[no-untyped-def]
            if argv[0] == "tailscale":
                return subprocess.CompletedProcess(
                    list(argv), 0, json.dumps(STATUS).encode(), b""
                )
            command = argv[-1]
            if argv[0] == "ssh" and command.startswith("docker exec"):
                if argv[-2] in self.down:
                    return subprocess.CompletedProcess(list(argv), 255, b"", b"down")
                if command.endswith("nginx -s reload"):
                    return subprocess.CompletedProcess(list(argv), 0, b"", b"")
                include = tmp_path / "b/access/operator-only.conf"
                text = MARKER + "\n"
                if include.exists():
                    text += include.read_text(encoding="utf-8")
                return subprocess.CompletedProcess(list(argv), 0, text.encode(), b"")
            return super().__call__(argv, stdin, timeout)

    home = tmp_path / "home"
    for entry in load_runtime().operator_reports.entries:
        for item in entry.data:
            path = home / item.relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Decision quality\n", encoding="utf-8")
    result = HandlerOperatorAccess(Gate(down=[HOST_A])).handle(
        ModelOperatorAccessRequest(home=str(home), overlay=_overlay(tmp_path)),
        environ={},
    )

    assert set(result.failed_targets) == {HOST_A}
    assert (tmp_path / "b/access/operator-only.conf").is_file()
    assert "reports/decision-quality/decision-quality.md" in result.published


def test_overlay_lists_the_url_of_every_serving_host_in_order(tmp_path: Path) -> None:
    overlay = _overlay(tmp_path)

    assert overlay.serve_urls == (
        "http://a.example.invalid:8088/",
        "http://b.example.invalid:8089/",
    )
    assert [t.ssh_target for t in overlay.targets] == [HOST_A, HOST_B]


def test_overlay_refuses_a_host_named_twice_and_an_absolute_local_copy(
    tmp_path: Path,
) -> None:
    base = _overlay(tmp_path).model_dump()
    twin = {**base["extra_targets"][0], "ssh_target": HOST_A}
    with pytest.raises(ValueError, match="once"):
        ModelFactoryFloorPublishOverlay.model_validate(
            {**base, "extra_targets": [twin]}
        )
    with pytest.raises(ValueError, match="relative"):
        ModelFactoryFloorPublishOverlay.model_validate(
            {**base, "local_copy_relpath": "/tmp/x"}
        )


def test_the_floor_reader_takes_the_first_serving_host_that_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_floor_simple_effect.handlers import (
        handler_floor_simple_tick as tick,
    )

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(
        "serve_url: http://a.example.invalid:8088/\n"
        "extra_targets:\n"
        "  - serve_url: http://b.example.invalid:8089/\n",
        encoding="utf-8",
    )
    env = {"ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY": str(overlay_file)}
    rt = tick.load_runtime()
    urls = tick.HandlerFloorSimpleTick._served_urls(rt, env)
    assert urls == (
        "http://a.example.invalid:8088/floor-simple.json",
        "http://b.example.invalid:8089/floor-simple.json",
    )
    asked: list[str] = []

    def fetch(url: str, timeout: int) -> set[str] | None:
        asked.append(url)
        return {"alarm-1"} if "b.example" in url else None

    monkeypatch.setattr(tick, "fetch_served_alarm_ids", fetch)
    read = tick.HandlerFloorSimpleTick()._served_reader(rt, env)

    assert read() == {"alarm-1"}
    assert asked == list(urls)
    asked.clear()
    # Failing control: with both hosts silent the reader reports unread rather than inventing an answer.
    monkeypatch.setattr(tick, "fetch_served_alarm_ids", lambda *_args, **_kwargs: None)
    assert tick.HandlerFloorSimpleTick()._served_reader(rt, env)() is None
