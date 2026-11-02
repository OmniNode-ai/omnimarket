# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator-only report is served to the owner's own devices and to nobody else (OMN-22119).

The web server's rule is the generated include followed by ``deny all``: the first ``allow`` that matches the
source address admits it, anything else is answered 403. ``served_status`` applies that rule to the include the
publish really wrote, so a device is served or refused by the file the server would read.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_operator_access import (
    HandlerOperatorAccess,
    render_include,
    resolve_operator_addresses,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelOperatorAccessRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.runtime_factory_floor_publish import (
    main,
)

pytestmark = pytest.mark.unit

OWNER = "owner@example.invalid"
OWNER_MAC = "192.0.2.10"
OWNER_PHONE = "192.0.2.11"
OWNER_SERVER = "192.0.2.12"
OTHER_MAC = "192.0.2.20"
STRANGER = "198.51.100.7"

STATUS = {
    "User": {
        "7": {"LoginName": OWNER},
        "8": {"LoginName": "other@example.invalid"},
    },
    "Self": {"UserID": 7, "OS": "macOS", "TailscaleIPs": [OWNER_MAC, "2001:db8::10"]},
    "Peer": {
        "k1": {"UserID": 7, "OS": "iOS", "TailscaleIPs": [OWNER_PHONE]},
        "k2": {"UserID": 7, "OS": "linux", "TailscaleIPs": [OWNER_SERVER]},
        "k3": {"UserID": 8, "OS": "macOS", "TailscaleIPs": [OTHER_MAC]},
    },
}
MARKER = load_runtime().operator_reports.access.gate_marker
REPORT = "reports/decision-quality/decision-quality.md"


def served_status(include: str, source: str) -> int:
    """HTTP status the server gives ``source``: the include's allow lines, then ``deny all``."""
    address = ipaddress.ip_address(source)
    for line in include.splitlines():
        line = line.strip()
        if (
            line.startswith("allow ")
            and line.endswith(";")
            and address == ipaddress.ip_address(line[len("allow ") : -1])
        ):
            return 200
    return 403


class Fleet:
    """Stands in for the publishing host's tailscale and for ssh to the serving host with its web server container."""

    def __init__(self, tmp_path: Path, *, carries_gate: bool = True) -> None:
        self.tmp_path = tmp_path
        self.carries_gate = carries_gate
        self.commands: list[str] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "tailscale":
            return subprocess.CompletedProcess(
                list(argv), 0, json.dumps(STATUS).encode(), b""
            )
        command = argv[-1]
        self.commands.append(command)
        if command.startswith("docker exec"):
            if command.endswith("nginx -s reload"):
                return subprocess.CompletedProcess(list(argv), 0, b"", b"")
            include = self.tmp_path / "access/operator-only.conf"
            text = (MARKER if self.carries_gate else "no gate here") + "\n"
            if include.exists():
                text += include.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, text.encode(), b"")
        return subprocess.run(
            ["sh", "-c", command], input=stdin, capture_output=True, check=False
        )

    def include(self) -> str:
        return (self.tmp_path / "access/operator-only.conf").read_text(encoding="utf-8")


def _overlay(tmp_path: Path) -> ModelFactoryFloorPublishOverlay:
    return ModelFactoryFloorPublishOverlay(
        ssh_target="lab-web",
        remote_dir=str(tmp_path / "site"),
        serve_url="http://example.invalid:8088/",
        bus_lane="lane-x",
        history_root=str(tmp_path / "kept"),
        owner_login=OWNER,
    )


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for entry in load_runtime().operator_reports.entries:
        for item in entry.data:
            path = home / item.relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Decision quality\n", encoding="utf-8")
    return home


def _request(tmp_path: Path) -> ModelOperatorAccessRequest:
    return ModelOperatorAccessRequest(
        home=str(_home(tmp_path)), overlay=_overlay(tmp_path)
    )


def test_owner_or_non_owner_or_missing_key_owner_devices_are_served(
    tmp_path: Path,
) -> None:
    fleet = Fleet(tmp_path)
    result = HandlerOperatorAccess(fleet).handle(_request(tmp_path), environ={})
    include = fleet.include()
    assert served_status(include, OWNER_MAC) == 200
    assert served_status(include, OWNER_PHONE) == 200
    assert served_status(include, "2001:db8::10") == 200
    assert result.admitted_devices == 3
    assert (tmp_path / "site" / REPORT).is_file()
    assert (tmp_path / "site/reports/decision-quality/index.html").is_file()
    manifest = json.loads(
        (tmp_path / "site/reports/decision-quality/manifest.json").read_text()
    )
    assert [r["id"] for r in manifest["reports"]] == ["decision_quality"]


def test_owner_or_non_owner_or_missing_key_non_owner_devices_are_refused(
    tmp_path: Path,
) -> None:
    fleet = Fleet(tmp_path)
    HandlerOperatorAccess(fleet).handle(_request(tmp_path), environ={})
    include = fleet.include()
    # another login's Mac, an address nobody lists, and the owner's own lab server: none is the operator's hand.
    assert served_status(include, OTHER_MAC) == 403
    assert served_status(include, STRANGER) == 403
    assert served_status(include, OWNER_SERVER) == 403
    assert "deny all;" not in include


def test_owner_or_non_owner_or_missing_key_addresses_come_from_the_status_not_the_source() -> (
    None
):
    only_phone = {
        **STATUS,
        "Self": {"UserID": 8, "OS": "macOS", "TailscaleIPs": [OTHER_MAC]},
    }
    assert resolve_operator_addresses(only_phone, OWNER, ("macOS", "iOS")) == (
        OWNER_PHONE,
    )
    assert resolve_operator_addresses(STATUS, "OWNER@example.invalid", ("iOS",)) == (
        OWNER_PHONE,
    )
    assert (
        resolve_operator_addresses(STATUS, "nobody@example.invalid", ("macOS",)) == ()
    )
    assert render_include(()).count("allow") == 0


def test_owner_or_non_owner_or_missing_key_the_gate_is_built_before_a_private_file_is_written(
    tmp_path: Path,
) -> None:
    fleet = Fleet(tmp_path)
    HandlerOperatorAccess(fleet).handle(_request(tmp_path), environ={})
    reload_at = next(i for i, c in enumerate(fleet.commands) if c.endswith("-s reload"))
    first_private = next(
        i for i, c in enumerate(fleet.commands) if "decision-quality" in c
    )
    include_at = next(
        i for i, c in enumerate(fleet.commands) if "operator-only.conf" in c
    )
    assert include_at < reload_at < first_private
    assert not (tmp_path / "site/operator-only.conf").exists()


def test_owner_or_non_owner_or_missing_key_a_server_without_the_gate_withdraws_the_report(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    (site / "reports/decision-quality").mkdir(parents=True)
    for name in ("index.html", "decision-quality.md", "manifest.json"):
        (site / "reports/decision-quality" / name).write_text("old", encoding="utf-8")
    (site / "index.html").write_text("shared", encoding="utf-8")
    fleet = Fleet(tmp_path, carries_gate=False)
    with pytest.raises(PublishRefusedError, match="does not carry the gate"):
        HandlerOperatorAccess(fleet).handle(_request(tmp_path), environ={})
    assert list((site / "reports/decision-quality").iterdir()) == []
    assert (site / "index.html").read_text() == "shared"


def test_owner_or_non_owner_or_missing_key_no_owner_device_withdraws_the_report(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    (site / "reports/decision-quality").mkdir(parents=True)
    (site / REPORT).write_text("old", encoding="utf-8")
    overlay = _overlay(tmp_path).model_copy(
        update={"owner_login": "nobody@example.invalid"}
    )
    request = ModelOperatorAccessRequest(home=str(_home(tmp_path)), overlay=overlay)
    with pytest.raises(PublishRefusedError, match="owner_login"):
        HandlerOperatorAccess(Fleet(tmp_path)).handle(request, environ={})
    assert not (site / REPORT).exists()


def test_owner_or_non_owner_or_missing_key_missing_overlay_key_refuses_the_publish_naming_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "ssh_target: lab-web\n"
        f"remote_dir: {tmp_path / 'site'}\n"
        "serve_url: http://example.invalid:8088/\n"
        "bus_lane: lane-x\n"
        f"history_root: {tmp_path / 'kept'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_PUBLISH_OVERLAY", str(overlay))
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    calls: list[Sequence[str]] = []

    def never(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")

    request = ModelOperatorAccessRequest(home=str(_home(tmp_path)))
    with pytest.raises(NodeOverlayError, match="owner_login"):
        HandlerOperatorAccess(never).handle(request)
    assert calls == []
    assert main(["--omni-home", str(_home(tmp_path)), "--skip-refresh"]) == 2
    captured = capsys.readouterr()
    assert "owner_login" in captured.err
    assert "published=" not in captured.out
    assert not (tmp_path / "site").exists()


def test_owner_or_non_owner_or_missing_key_a_blank_or_malformed_key_is_refused_naming_it() -> (
    None
):
    for bad in ("", "no-at-sign", "two words@example.invalid"):
        with pytest.raises(ValueError, match="owner_login"):
            ModelFactoryFloorPublishOverlay(
                ssh_target="lab-web",
                remote_dir="/srv/site",
                serve_url="u",
                bus_lane="lane-x",
                history_root="/srv/site",
                owner_login=bad,
            )


def test_owner_or_non_owner_or_missing_key_the_shared_site_never_names_the_report() -> (
    None
):
    rt = load_runtime()
    shared = [d.remote for e in rt.reports.entries for d in (*e.data,)] + [
        p.remote for e in rt.reports.entries for p in e.pages
    ]
    assert not [n for n in shared if "decision-quality" in n]
    assert {e.id for e in rt.reports.entries}.isdisjoint(
        {e.id for e in rt.operator_reports.entries}
    )
    assert rt.operator_reports.manifest.startswith("reports/decision-quality/")
