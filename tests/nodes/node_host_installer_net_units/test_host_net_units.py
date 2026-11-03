# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_net_units: root-owned host files of a Linux pool host, declared in an overlay and installed
byte for byte, with the network interface read from the host and sshd validated before it is reloaded.

Every payload, host name and address below is fixture input written into a temporary overlay; the node holds none.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_net_units"

SCRIPT = "#!/usr/bin/python3\nprint('assert')\n"
TEMPLATE = (
    "[Service]\nExecStart=/usr/local/sbin/asserter __INTERFACE__ 5\nRestart=always\n"
)
SESSIONS = "MaxStartups 100:30:300\nClientAliveInterval 60\n"
HARDENING = "PasswordAuthentication no\nPermitRootLogin no\n"

# The host fixtures: what `ip` prints on each host. The interface names live here, as fixture input only.
H_ONE = {
    "route": "default via 198.51.100.1 dev eth7 proto static metric 100 \n198.51.100.0/24 dev eth7 proto kernel scope link src 198.51.100.53\n",
    "addr": {
        "eth7": "6: eth7    inet 198.51.100.53/24 brd 198.51.100.255 scope global eth7\\       valid_lft forever\n"
    },
    "iface": "eth7",
}
H_TWO = {
    "route": "default via 198.51.100.1 dev ens4 proto dhcp src 198.51.100.51 metric 100 \n",
    "addr": {
        "ens4": "3: ens4    inet 198.51.100.51/24 brd 198.51.100.255 scope global ens4\\       valid_lft forever\n"
    },
    "iface": "ens4",
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_overlay(path: Path, rendered_one: str, rendered_two: str) -> Path:
    path.mkdir(parents=True)
    (path / "asserter").write_text(SCRIPT)
    (path / "asserter.service.tmpl").write_text(TEMPLATE)
    (path / "sessions.conf").write_text(SESSIONS)
    (path / "hardening.conf").write_text(HARDENING)
    document: dict[str, Any] = {
        "schema": "host_net_units.v1",
        "units": {
            "asserter": {
                "kind": "systemd-system-service",
                "platform": "linux",
                "hosts": ["pool-a", "pool-b"],
                "service": "asserter.service",
                "files": [
                    {
                        "source": "asserter",
                        "destination": "/usr/local/sbin/asserter",
                        "mode": "0755",
                        "sha256": sha(SCRIPT),
                    },
                    {
                        "source": "asserter.service.tmpl",
                        "destination": "/etc/systemd/system/asserter.service",
                        "mode": "0644",
                        "render": "interface",
                        "sha256_by_host": {"pool-a": sha(rendered_one)},
                    },
                ],
            },
            "limits": {
                "kind": "sshd-dropin",
                "platform": "linux",
                "hosts": ["pool-a", "pool-b"],
                "files": [
                    {
                        "source": "sessions.conf",
                        "destination": "/etc/ssh/sshd_config.d/10-limits.conf",
                        "mode": "0644",
                        "sha256": sha(SESSIONS),
                    }
                ],
            },
            "hardening": {
                "kind": "sshd-dropin",
                "platform": "linux",
                "hosts": ["pool-a", "pool-b"],
                "files": [
                    {
                        "source": "hardening.conf",
                        "destination": "/etc/ssh/sshd_config.d/00-hardening.conf",
                        "mode": "0644",
                        "sha256": sha(HARDENING),
                    }
                ],
            },
        },
    }
    (path / "units.yaml").write_text(yaml.safe_dump(document))
    return path


class FakeHost:
    """The command side of a host: `ip`, `systemctl` and `sshd`, recorded; the file side is a real directory."""

    def __init__(self, facts: dict[str, Any], sshd_ok: bool = True) -> None:
        self.facts = facts
        self.sshd_ok = sshd_ok
        self.calls: list[list[str]] = []
        self.active = False

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        name = Path(argv[0]).name
        if name == "ip" and argv[1:4] == ["-4", "route", "show"]:
            return subprocess.CompletedProcess(argv, 0, self.facts["route"], "")
        if name == "ip" and "addr" in argv:
            dev = argv[argv.index("dev") + 1]
            return subprocess.CompletedProcess(
                argv, 0, self.facts["addr"].get(dev, ""), ""
            )
        if name == "sshd":
            return subprocess.CompletedProcess(
                argv,
                0 if self.sshd_ok else 255,
                "",
                "" if self.sshd_ok else "bad config\n",
            )
        if name == "systemctl":
            verb = argv[1]
            if verb == "is-active":
                return subprocess.CompletedProcess(
                    argv,
                    0 if self.active else 3,
                    "active\n" if self.active else "inactive\n",
                    "",
                )
            if verb in ("enable", "start", "restart"):
                self.active = True
            if verb == "disable":
                self.active = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls if Path(c[0]).name == "systemctl"]


def handler(host: FakeHost, fs: Path, system: str = "Linux") -> HandlerNetUnits:
    return HandlerNetUnits(run=host, system=lambda: system, sudo=[], fs_root=fs)


def request(
    overlay: Path, unit: str, mode: str = "install", host_name: str = "pool-a"
) -> ModelNetUnitsRequest:
    return ModelNetUnitsRequest(
        unit=unit, mode=mode, host_name=host_name, overlay_root=str(overlay)
    )


@pytest.fixture
def rendered_one() -> str:
    return TEMPLATE.replace("__INTERFACE__", H_ONE["iface"])


@pytest.fixture
def rendered_two() -> str:
    return TEMPLATE.replace("__INTERFACE__", H_TWO["iface"])


@pytest.fixture
def overlay(tmp_path: Path, rendered_one: str, rendered_two: str) -> Path:
    return make_overlay(tmp_path / "overlay", rendered_one, rendered_two)


def test_host_net_payloads_install_byte_for_byte(
    tmp_path: Path, overlay: Path, rendered_one: str
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    h = handler(host, fs)
    for unit in ("asserter", "limits", "hardening"):
        result = h.handle(request(overlay, unit))
        assert result.status == "success", result.stderr
    assert (fs / "usr/local/sbin/asserter").read_bytes() == SCRIPT.encode()
    assert (fs / "usr/local/sbin/asserter").stat().st_mode & 0o777 == 0o755
    unit_file = fs / "etc/systemd/system/asserter.service"
    assert unit_file.read_bytes() == rendered_one.encode()
    assert hashlib.sha256(unit_file.read_bytes()).hexdigest() == sha(rendered_one)
    assert (
        fs / "etc/ssh/sshd_config.d/10-limits.conf"
    ).read_bytes() == SESSIONS.encode()
    assert (
        fs / "etc/ssh/sshd_config.d/00-hardening.conf"
    ).read_bytes() == HARDENING.encode()
    assert host.active is True


def test_host_net_payload_that_misses_its_digest_is_refused(
    tmp_path: Path, overlay: Path
) -> None:
    (overlay / "sessions.conf").write_text(SESSIONS + "# edited\n")
    fs = tmp_path / "fs"
    result = handler(FakeHost(H_ONE), fs).handle(request(overlay, "limits"))
    assert result.status == "refused"
    assert result.reason == "digest-mismatch"
    assert not (fs / "etc/ssh/sshd_config.d/10-limits.conf").exists()


def test_host_net_destination_outside_the_contract_roots_is_refused(
    tmp_path: Path, overlay: Path
) -> None:
    document = yaml.safe_load((overlay / "units.yaml").read_text())
    document["units"]["limits"]["files"][0]["destination"] = "/etc/passwd"
    (overlay / "units.yaml").write_text(yaml.safe_dump(document))
    result = handler(FakeHost(H_ONE), tmp_path / "fs").handle(
        request(overlay, "limits")
    )
    assert (result.status, result.reason) == ("refused", "destination-not-allowed")


def test_host_net_interface_comes_from_the_host(
    tmp_path: Path, overlay: Path, rendered_one: str, rendered_two: str
) -> None:
    one = handler(FakeHost(H_ONE), tmp_path / "a").handle(
        request(overlay, "asserter", "dry-run", "pool-a")
    )
    two = handler(FakeHost(H_TWO), tmp_path / "b").handle(
        request(overlay, "asserter", "dry-run", "pool-b")
    )
    assert one.rendered == rendered_one
    assert two.rendered == rendered_two
    # the two rendered units differ in the interface word alone
    assert one.rendered.replace(H_ONE["iface"], "@") == two.rendered.replace(
        H_TWO["iface"], "@"
    )
    assert H_ONE["iface"] != H_TWO["iface"]


def test_host_net_interface_rule_is_the_lowest_metric_default_route(
    tmp_path: Path, overlay: Path
) -> None:
    facts = {
        "route": "default via 1.1.1.1 dev wan0 metric 600\ndefault via 198.51.100.1 dev eth7 metric 100\n",
        "addr": {
            "eth7": H_ONE["addr"]["eth7"],
            "wan0": "9: wan0    inet 1.1.1.9/24 scope global wan0\n",
        },
    }
    result = handler(FakeHost(facts), tmp_path / "fs").handle(
        request(overlay, "asserter", "dry-run")
    )
    assert "eth7" in result.rendered
    assert "wan0" not in result.rendered


def test_host_net_interface_refused_without_default_route_or_address(
    tmp_path: Path, overlay: Path
) -> None:
    none = {"route": "", "addr": {}}
    result = handler(FakeHost(none), tmp_path / "a").handle(
        request(overlay, "asserter", "dry-run")
    )
    assert (result.status, result.reason) == ("refused", "no-default-route")
    bare = {"route": H_ONE["route"], "addr": {}}
    result = handler(FakeHost(bare), tmp_path / "b").handle(
        request(overlay, "asserter", "dry-run")
    )
    assert (result.status, result.reason) == ("refused", "interface-has-no-ipv4")


def test_host_net_interface_no_declared_text_names_an_interface(overlay: Path) -> None:
    named = re.compile(r"\b(eno|ens|enp|eth)\d")
    scanned = [
        p
        for p in NODE_DIR.rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".tmpl", ".md"}
    ]
    assert any(p.name == "contract.yaml" for p in scanned)
    offenders = [str(p) for p in scanned if named.search(p.read_text())]
    assert offenders == []
    # the overlay fixture's own template carries the placeholder, never a name
    assert "__INTERFACE__" in (overlay / "asserter.service.tmpl").read_text()
    assert not named.search((overlay / "asserter.service.tmpl").read_text())


def test_host_net_idempotent_second_install_writes_and_reloads_nothing(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    h = handler(host, fs)
    for unit in ("asserter", "limits", "hardening"):
        assert h.handle(request(overlay, unit)).status == "success"
    stamps = {p: p.stat().st_mtime_ns for p in fs.rglob("*") if p.is_file()}
    host.calls.clear()
    for unit in ("asserter", "limits", "hardening"):
        result = h.handle(request(overlay, unit))
        assert result.status == "success", unit
        assert result.changed is False, unit
        assert "no change" in result.stdout
    assert {p: p.stat().st_mtime_ns for p in fs.rglob("*") if p.is_file()} == stamps
    assert [v for v in host.verbs() if "is-active" not in v] == []
    assert not any(Path(c[0]).name == "sshd" for c in host.calls)


def test_host_net_idempotent_dry_run_reports_no_change_when_installed_files_match(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    h = handler(host, fs)
    for unit in ("asserter", "limits", "hardening"):
        h.handle(request(overlay, unit))
    for unit in ("asserter", "limits", "hardening"):
        result = h.handle(request(overlay, unit, "dry-run"))
        assert result.changed is False
        assert "no change" in result.stdout
    changed = fs / "etc/ssh/sshd_config.d/10-limits.conf"
    changed.write_text("different\n")
    result = h.handle(request(overlay, "limits", "dry-run"))
    assert result.changed is True
    assert "would write" in result.stdout
    assert changed.read_text() == "different\n"


def test_host_net_idempotent_failed_sshd_check_restores_the_original_and_reloads_nothing(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    target = fs / "etc/ssh/sshd_config.d/10-limits.conf"
    target.parent.mkdir(parents=True)
    target.write_text("MaxStartups 3\n")
    target.chmod(0o640)
    host = FakeHost(H_ONE, sshd_ok=False)
    result = handler(host, fs).handle(request(overlay, "limits"))
    assert result.status == "failure"
    assert "sshd -t" in result.stderr
    assert target.read_text() == "MaxStartups 3\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not any(
        c[1:2] == ["reload"] for c in host.calls if Path(c[0]).name == "systemctl"
    )
    # and a drop-in that did not exist is removed again
    fresh = fs / "etc/ssh/sshd_config.d/00-hardening.conf"
    result = handler(host, fs).handle(request(overlay, "hardening"))
    assert result.status == "failure"
    assert not fresh.exists()
    assert not any(
        c[1:2] == ["reload"] for c in host.calls if Path(c[0]).name == "systemctl"
    )


def test_host_net_idempotent_sshd_is_reloaded_never_restarted(
    tmp_path: Path, overlay: Path
) -> None:
    host = FakeHost(H_ONE)
    host.active = True  # the sshd service
    result = handler(host, tmp_path / "fs").handle(request(overlay, "limits"))
    assert result.status == "success"
    systemctl = [c for c in host.calls if Path(c[0]).name == "systemctl"]
    assert any(c[1] == "reload" for c in systemctl)
    assert not any(c[1] in ("restart", "stop") for c in systemctl)
    sshd_at = next(i for i, c in enumerate(host.calls) if Path(c[0]).name == "sshd")
    reload_at = next(i for i, c in enumerate(host.calls) if c[1:2] == ["reload"])
    assert sshd_at < reload_at


def test_host_net_idempotent_a_non_linux_host_is_refused(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    result = handler(host, fs, system="Darwin").handle(request(overlay, "limits"))
    assert (result.status, result.reason) == ("refused", "platform-not-linux")
    assert host.calls == []
    assert not fs.exists()


def test_host_net_idempotent_a_host_the_unit_does_not_name_is_refused(
    tmp_path: Path, overlay: Path
) -> None:
    host = FakeHost(H_ONE)
    result = handler(host, tmp_path / "fs").handle(
        request(overlay, "limits", host_name="pool-z")
    )
    assert (result.status, result.reason) == ("refused", "host-not-declared")
    assert host.calls == []


def test_host_net_idempotent_uninstall_removes_only_the_declared_files(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    h = handler(host, fs)
    for unit in ("asserter", "limits", "hardening"):
        h.handle(request(overlay, unit))
    bystander = fs / "etc/ssh/sshd_config.d/50-other.conf"
    bystander.write_text("X\n")
    for unit in ("asserter", "limits"):
        assert h.handle(request(overlay, unit, "uninstall")).status == "success"
    assert not (fs / "usr/local/sbin/asserter").exists()
    assert not (fs / "etc/systemd/system/asserter.service").exists()
    assert not (fs / "etc/ssh/sshd_config.d/10-limits.conf").exists()
    assert (
        fs / "etc/ssh/sshd_config.d/00-hardening.conf"
    ).exists()  # a unit not uninstalled stays
    assert bystander.read_text() == "X\n"
    assert host.active is False


def test_host_net_status_reports_each_file_and_the_service(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    host = FakeHost(H_ONE)
    h = handler(host, fs)
    absent = h.handle(request(overlay, "asserter", "status"))
    assert absent.exit_code == 1
    assert "absent" in absent.stdout
    h.handle(request(overlay, "asserter"))
    ok = h.handle(request(overlay, "asserter", "status"))
    assert ok.exit_code == 0
    assert "identical" in ok.stdout
    assert "active" in ok.stdout
