# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shape followed: tests/nodes/node_host_installer_net_units/test_host_net_units.py and
tests/nodes/node_host_installer_net_units/test_golden_chain_host_net_units.py.

Secret-bearing files and the ``nut-config`` kind: the planted secret lands in the installed
file and in nothing else -- never in a result field, a digest or a dry-run line -- the
sha256 pin is the pin of the template BEFORE the secret is filled, the file is installed in
group ``nut``, and an ``active_services`` unit that stays down fails the install.

Every host name, address, user and secret below is fixture input written into a temporary
overlay; the node holds none.
"""

from __future__ import annotations

import copy
import hashlib
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_net_units_overlay import ModelNetUnitsOverlay
from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
    SecretUnavailableError,
    resolve_secret,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
    ModelNetUnitsResult,
)
from omnimarket.nodes.node_host_installer_net_units.node import (
    NodeHostInstallerNetUnits,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PROJECT,
    FakeState,
    running_fake_store,
)
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    PLANTED as STORE_PLANTED,
)
from tests.unit.nodes.node_secret_store_effect.test_http_fake_store import _onex_home

ROOT = Path(__file__).resolve().parents[3]

#: A planted secret value: fixture input returned by the injected ``secret_reader``.
PLANTED = "planted-nut-value-7d3c-do-not-leak"
NUT_CONF_CONTENT = "MODE=__MODE__\n"
UPS_USERS_CONTENT = (
    "[__USER__]\n    password = __SECRET_NUT_PRIMARY_PASSWORD__\n    upsmon primary\n"
)
#: The two store paths the secret store's contract declares for the NUT monitor users; the
#: overlays repository's tests/test_nut_ups_overlay.py spells the same list, so changing
#: one side turns one test red there and one red here.
EXPECTED_NUT_SECRETS = (
    "/shared/env/NUT_PRIMARY_PASSWORD",
    "/shared/env/NUT_SECONDARY_PASSWORD",
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def make_overlay(
    path: Path,
    *,
    pin: str | None = None,
    upsd_content: str = UPS_USERS_CONTENT,
) -> Path:
    """The fixture overlay: the nut-config unit plus a plain systemd unit for the default group.

    Both payloads are inline ``content``. ``pin`` overrides the upsd.users pin (the
    failing control pins the secret-FILLED bytes); by default the pin is of the template
    AFTER the values are filled and BEFORE the secret, which is exactly ``upsd_content``
    with ``__USER__`` replaced and the secret placeholder still present.
    """
    path.mkdir(parents=True)
    document: dict[str, Any] = {
        "schema": "host_net_units.v1",
        "units": {
            "nut-fixture": {
                "kind": "nut-config",
                "platform": "linux",
                "hosts": ["pool-a"],
                "service": "nut.target",
                "active_services": ["nut-monitor.service"],
                "files": [
                    {
                        "destination": "/etc/nut/nut.conf",
                        "mode": "0640",
                        "content": NUT_CONF_CONTENT,
                        "render": "values",
                        "values": {"MODE": "netserver"},
                        "sha256_by_host": {"pool-a": sha("MODE=netserver\n")},
                    },
                    {
                        "destination": "/etc/nut/upsd.users",
                        "mode": "0640",
                        "content": upsd_content,
                        "render": "values",
                        "values": {"USER": "upsmon_primary"},
                        "secrets": {
                            "NUT_PRIMARY_PASSWORD": {
                                "folder": "/shared/env",
                                "key": "NUT_PRIMARY_PASSWORD",
                            }
                        },
                        "sha256_by_host": {
                            "pool-a": pin
                            if pin is not None
                            else sha(upsd_content.replace("__USER__", "upsmon_primary"))
                        },
                    },
                ],
            },
            "x-service": {
                "kind": "systemd-system-service",
                "platform": "linux",
                "hosts": ["pool-a"],
                "service": "x.service",
                "files": [
                    {
                        "destination": "/etc/systemd/system/x.service",
                        "mode": "0644",
                        "content": "[Service]\nExecStart=/bin/true\n",
                    }
                ],
            },
        },
    }
    (path / "units.yaml").write_text(yaml.safe_dump(document))
    return path


class FakeHost:
    """The command side of a host: ``systemctl`` and ``install``, recorded; the file side
    is a real directory. ``enable``/``start``/``restart`` mark the unit active, except the
    units in ``refuses``, which stay down no matter what is run against them."""

    def __init__(
        self,
        active: dict[str, bool] | None = None,
        refuses: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.states: dict[str, bool] = {"nut-monitor.service": True}
        self.states.update(active or {})
        self.refuses = set(refuses or ())

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        rest = argv[1:] if argv[0] == "sudo" else argv
        name = Path(rest[0]).name
        if name == "systemctl":
            verb, unit = rest[1], rest[-1]
            if verb == "is-active":
                up = self.states.get(unit, False)
                return subprocess.CompletedProcess(
                    argv, 0 if up else 3, "active\n" if up else "inactive\n", ""
                )
            if verb in ("enable", "start", "restart") and unit not in self.refuses:
                self.states[unit] = True
            if verb == "disable":
                self.states[unit] = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if name == "install":
            staged, dest = Path(rest[-2]), Path(rest[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(staged.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def reader_planted() -> tuple[Callable[[str, str], str], list[tuple[str, str]]]:
    """A ``secret_reader`` that answers the planted value and records every (folder, key)."""
    calls: list[tuple[str, str]] = []

    def reader(folder: str, key: str) -> str:
        calls.append((folder, key))
        return PLANTED

    return reader, calls


def handler(
    host: FakeHost,
    fs: Path,
    reader: Callable[[str, str], str],
    sudo: list[str] | None = None,
) -> HandlerNetUnits:
    return HandlerNetUnits(
        run=host,
        system=lambda: "Linux",
        sudo=sudo or [],
        fs_root=fs,
        secret_reader=reader,
    )


def request(overlay: Path, unit: str, mode: str = "install") -> ModelNetUnitsRequest:
    return ModelNetUnitsRequest(
        unit=unit, mode=mode, host_name="pool-a", overlay_root=str(overlay)
    )


def _assert_no_planted(result: ModelNetUnitsResult) -> None:
    """The planted secret is in no field of the result, serialized or not."""
    for field in (
        result.stdout,
        result.stderr,
        result.reason,
        result.rendered,
        result.model_dump_json(),
    ):
        assert PLANTED not in field


def _nothing_written(fs: Path) -> None:
    assert not fs.exists() or not any(fs.rglob("*"))


def _refused(result: ModelNetUnitsResult, reason: str, fs: Path) -> None:
    assert result.status == "refused"
    assert result.reason == reason
    _assert_no_planted(result)
    _nothing_written(fs)


@pytest.fixture
def overlay(tmp_path: Path) -> Path:
    return make_overlay(tmp_path / "overlay")


@pytest.fixture
def fake_store() -> Iterator[tuple[str, FakeState]]:
    with running_fake_store() as served:
        yield served


def test_golden_chain_installs_the_secret_through_the_node(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    reader, calls = reader_planted()
    node = NodeHostInstallerNetUnits()
    node.handler = handler(FakeHost(), fs, reader)
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_host_installer_net_units"

    result = node.handle(request(overlay, "nut-fixture"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    assert calls == [("/shared/env", "NUT_PRIMARY_PASSWORD")]
    installed = fs / "etc/nut/upsd.users"
    # the positive control: the secret DID land in the file, and only there
    assert installed.read_text() == (
        "[upsmon_primary]\n    password = " + PLANTED + "\n    upsmon primary\n"
    )
    assert (fs / "etc/nut/nut.conf").read_text() == "MODE=netserver\n"

    again = node.handle(request(overlay, "nut-fixture"))
    assert again.status == "success"
    assert again.changed is False
    assert "no change" in again.stdout


def test_secret_is_never_printed(tmp_path: Path, overlay: Path) -> None:
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    h = handler(FakeHost(), fs, reader)
    # the dry-run comes first: it is the mode whose lines would carry a digest
    dry = h.handle(request(overlay, "nut-fixture", "dry-run"))
    install = h.handle(request(overlay, "nut-fixture"))
    status = h.handle(request(overlay, "nut-fixture", "status"))
    for result in (dry, install, status):
        _assert_no_planted(result)
    assert install.digests == {"/etc/nut/nut.conf": sha("MODE=netserver\n")}
    lines = dry.stdout.splitlines()
    upsd = next(line for line in lines if "upsd.users" in line)
    nut = next(line for line in lines if "nut.conf" in line)
    assert "would write /etc/nut/upsd.users" in upsd
    assert "content withheld" in upsd
    assert "sha256" in nut


def test_nut_config_installs_group_nut_and_the_default_stays_root(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    host = FakeHost()
    h = handler(host, fs, reader, sudo=["sudo"])
    assert h.handle(request(overlay, "nut-fixture")).status == "success"
    assert h.handle(request(overlay, "x-service")).status == "success"

    def tool(argv: list[str]) -> str:
        return Path(argv[1] if argv[0] == "sudo" else argv[0]).name

    installs = [c for c in host.calls if tool(c) == "install"]
    nut = next(c for c in installs if c[-1].endswith("/etc/nut/upsd.users"))
    assert nut[nut.index("-g") + 1] == "nut"
    assert nut[nut.index("-o") + 1] == "root"
    plain = next(c for c in installs if c[-1].endswith("/etc/systemd/system/x.service"))
    assert plain[plain.index("-g") + 1] == "root"
    assert plain[plain.index("-o") + 1] == "root"


def test_pin_of_the_secret_filled_bytes_is_refused(tmp_path: Path) -> None:
    # the failing control: the pin names the bytes AFTER the secret is filled, so it is wrong
    filled = "[upsmon_primary]\n    password = " + PLANTED + "\n    upsmon primary\n"
    overlay = make_overlay(tmp_path / "overlay", pin=sha(filled))
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "digest-mismatch", fs)


def test_pin_of_the_template_before_the_secret_installs(
    tmp_path: Path, overlay: Path
) -> None:
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    assert result.status == "success", result.stderr
    installed = fs / "etc/nut/upsd.users"
    assert installed.exists()
    # the installed bytes are the FILLED ones, so they no longer hash to the pinned template
    assert hashlib.sha256(installed.read_bytes()).hexdigest() != sha(
        UPS_USERS_CONTENT.replace("__USER__", "upsmon_primary")
    )


def test_secret_unresolved_is_refused(tmp_path: Path, overlay: Path) -> None:
    fs = tmp_path / "fs"

    def reader(folder: str, key: str) -> str:
        raise SecretUnavailableError("not_found")

    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "secret-unresolved", fs)
    assert "/shared/env/NUT_PRIMARY_PASSWORD: not_found" in result.stderr


@pytest.mark.parametrize("value", ["has a space", ""])
def test_secret_unsafe_value_is_refused(
    tmp_path: Path, overlay: Path, value: str
) -> None:
    fs = tmp_path / "fs"

    def reader(folder: str, key: str) -> str:
        return value

    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "secret-unsafe-value", fs)


def test_secret_without_placeholder_is_refused(tmp_path: Path) -> None:
    # the one mutated thing: the text carries no secret placeholder (the pin follows the text)
    overlay = make_overlay(
        tmp_path / "overlay", upsd_content="[__USER__]\n    password = x\n"
    )
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "secret-without-placeholder", fs)


def test_secret_placeholder_undeclared_is_refused(tmp_path: Path) -> None:
    # the one mutated thing: the text carries a second placeholder the secrets map does not name
    overlay = make_overlay(
        tmp_path / "overlay",
        upsd_content=(
            "[__USER__]\n    password = __SECRET_NUT_PRIMARY_PASSWORD__\n"
            "    upsmon primary __SECRET_OTHER__\n"
        ),
    )
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "secret-placeholder-undeclared", fs)


@pytest.mark.parametrize("destination", ["/etc/nutx/a.conf", "/usr/local/sbin/x"])
def test_nut_config_destination_outside_etc_nut_is_refused(
    tmp_path: Path, destination: str
) -> None:
    overlay = make_overlay(tmp_path / "overlay")
    document = yaml.safe_load((overlay / "units.yaml").read_text())
    document["units"]["nut-fixture"]["files"][0]["destination"] = destination
    (overlay / "units.yaml").write_text(yaml.safe_dump(document))
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "destination-not-allowed", fs)


def test_nut_config_unit_without_service_is_refused(tmp_path: Path) -> None:
    overlay = make_overlay(tmp_path / "overlay")
    document = yaml.safe_load((overlay / "units.yaml").read_text())
    del document["units"]["nut-fixture"]["service"]
    (overlay / "units.yaml").write_text(yaml.safe_dump(document))
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    result = handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture"))
    _refused(result, "service-not-declared", fs)


def test_active_service_down_fails_the_install(tmp_path: Path, overlay: Path) -> None:
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    # this host refuses to mark the monitor active, so it stays down after the install
    host = FakeHost(
        active={"nut-monitor.service": False}, refuses={"nut-monitor.service"}
    )
    result = handler(host, fs, reader).handle(request(overlay, "nut-fixture"))
    assert result.exit_code == 1
    assert result.status == "failure"
    assert "nut-monitor.service" in result.stderr
    _assert_no_planted(result)


def test_status_reports_the_active_service(tmp_path: Path, overlay: Path) -> None:
    fs = tmp_path / "fs"
    reader, _ = reader_planted()
    host = FakeHost()
    h = handler(host, fs, reader)
    assert h.handle(request(overlay, "nut-fixture")).status == "success"
    up = h.handle(request(overlay, "nut-fixture", "status"))
    assert up.exit_code == 0
    assert "nut-monitor.service active" in up.stdout
    down_host = FakeHost(
        active={"nut.target": True, "nut-monitor.service": False},
        refuses={"nut-monitor.service"},
    )
    down = handler(down_host, fs, reader).handle(
        request(overlay, "nut-fixture", "status")
    )
    assert down.exit_code == 1
    assert "nut-monitor.service NOT RUNNING: inactive" in down.stdout


def test_model_accepts_the_fixture_document_and_refuses_bad_secrets(
    overlay: Path,
) -> None:
    document = yaml.safe_load((overlay / "units.yaml").read_text())
    ModelNetUnitsOverlay.model_validate(document)

    bad = copy.deepcopy(document)
    bad["units"]["nut-fixture"]["files"][1]["secrets"]["NUT_PRIMARY_PASSWORD"][
        "folder"
    ] = "shared/env"
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(bad)

    extra = copy.deepcopy(document)
    extra["units"]["nut-fixture"]["files"][0]["unknown_field"] = 1
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(extra)


def test_resolve_secret_seam_reads_the_real_store_handler(
    tmp_path: Path, fake_store: tuple[str, FakeState]
) -> None:
    address, state = fake_store
    state.secrets[("/shared/env", "NUT_PRIMARY_PASSWORD")] = STORE_PLANTED
    store = HandlerSecretStore(onex_home=_onex_home(tmp_path, address))
    assert (
        resolve_secret("/shared/env", "NUT_PRIMARY_PASSWORD", store_handler=store)
        == STORE_PLANTED
    )
    # the secondary key is generated by the contract but stored by no fixture here
    with pytest.raises(SecretUnavailableError) as raised:
        resolve_secret("/shared/env", "NUT_SECONDARY_PASSWORD", store_handler=store)
    assert raised.value.outcome == "not_found"


def test_expected_nut_secrets_match_the_contract_generated_keys() -> None:
    contract = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
        ).read_text()
    )
    generated = contract["generated_keys"]

    def spelled(mapping: dict[str, Any]) -> tuple[str, str]:
        primary = mapping["nut_primary_password"]
        secondary = mapping["nut_secondary_password"]
        return (
            f"{primary['folder']}/{primary['key']}",
            f"{secondary['folder']}/{secondary['key']}",
        )

    assert spelled(generated) == EXPECTED_NUT_SECRETS
    # the failing control: one renamed key and the spelling no longer matches
    renamed = copy.deepcopy(generated)
    renamed["nut_primary_password"]["key"] = "RENAMED"
    assert spelled(renamed) != EXPECTED_NUT_SECRETS


def _document_with_store(overlay: Path, address: str) -> None:
    """Add the document-level ``secret_store`` block the installer reads when no reader is injected."""
    units = overlay / "units.yaml"
    document = yaml.safe_load(units.read_text())
    document["secret_store"] = {
        "provider": "infisical",
        "address": address,
        "project_id": PROJECT,
        "environment": "dev",
        "plain_http_hosts": ["127.0.0.1"],
        "identity_file": ".omnibase/.env",
        "client_id_ref": "INFISICAL_CLIENT_ID",
        "client_secret_ref": "INFISICAL_CLIENT_SECRET",
    }
    units.write_text(yaml.safe_dump(document))


def _identity_home(home: Path) -> None:
    env = home / ".omnibase" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text(
        f"INFISICAL_CLIENT_ID={CLIENT_ID}\nINFISICAL_CLIENT_SECRET={CLIENT_SECRET}\n"
    )
    env.chmod(0o600)


def test_real_path_the_documents_secret_store_block_fills_the_file(
    tmp_path: Path,
    overlay: Path,
    fake_store: tuple[str, FakeState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reader is injected: the document's own ``secret_store`` block addresses the real store handler."""
    address, state = fake_store
    state.secrets[("/shared/env", "NUT_PRIMARY_PASSWORD")] = STORE_PLANTED
    _document_with_store(overlay, address)
    _identity_home(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fs = tmp_path / "fs"
    node = HandlerNetUnits(run=FakeHost(), system=lambda: "Linux", sudo=[], fs_root=fs)
    result = node.handle(request(overlay, "nut-fixture"))
    assert result.status == "success", result.stderr
    assert STORE_PLANTED in (fs / "etc/nut/upsd.users").read_text()
    for field in (
        result.stdout,
        result.stderr,
        result.reason,
        result.model_dump_json(),
    ):
        assert STORE_PLANTED not in field


def test_real_path_a_key_the_store_does_not_hold_is_refused_and_writes_nothing(
    tmp_path: Path,
    overlay: Path,
    fake_store: tuple[str, FakeState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address, _state = fake_store
    _document_with_store(overlay, address)
    _identity_home(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fs = tmp_path / "fs"
    node = HandlerNetUnits(run=FakeHost(), system=lambda: "Linux", sudo=[], fs_root=fs)
    result = node.handle(request(overlay, "nut-fixture"))
    assert result.status == "refused"
    assert result.reason == "secret-unresolved"
    assert "/shared/env/NUT_PRIMARY_PASSWORD: not_found" in result.stderr
    _nothing_written(fs)


def test_real_path_a_machine_with_no_store_block_is_refused(
    tmp_path: Path, overlay: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control of the document block: without it the installer reaches no store."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fs = tmp_path / "fs"
    node = HandlerNetUnits(run=FakeHost(), system=lambda: "Linux", sudo=[], fs_root=fs)
    result = node.handle(request(overlay, "nut-fixture"))
    assert result.status == "refused"
    assert result.reason == "secret-unresolved"
    _nothing_written(fs)


def test_a_root_only_installed_file_is_read_through_sudo_and_compared(
    tmp_path: Path, overlay: Path
) -> None:
    """The first install, then the file turns unreadable to the caller (root:nut 0640): the second run reads it through
    `sudo cat`, finds its bytes identical to the declaration."""
    fs = tmp_path / "fs"
    reader, _calls = reader_planted()
    node = handler(FakeHost(), fs, reader)
    assert node.handle(request(overlay, "nut-fixture")).changed is True
    installed = fs / "etc/nut/upsd.users"
    content = installed.read_text()
    unreadable = fs / "etc/nut/nut.conf"
    unreadable_text = unreadable.read_text()

    class CatHost(FakeHost):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["sudo", "cat"]:
                self.calls.append(argv)
                texts = {str(installed): content, str(unreadable): unreadable_text}
                return subprocess.CompletedProcess(argv, 0, texts[argv[2]], "")
            return super().__call__(argv)

    cat_host = CatHost()
    cat_host.states["nut.target"] = True  # the first install started it
    installed.chmod(0)
    unreadable.chmod(0)
    try:
        again = handler(cat_host, fs, reader, sudo=["sudo"]).handle(
            request(overlay, "nut-fixture", mode="dry-run")
        )
    finally:
        installed.chmod(0o640)
        unreadable.chmod(0o640)
    assert again.status == "success", again.stderr
    # the fixture can only make the file unreadable by dropping its mode, so the one difference left is the mode: the
    # bytes read through `sudo cat` are identical to the declaration
    assert "(mode," in again.stdout
    assert "(differs" not in again.stdout
    assert "(absent" not in again.stdout
    assert ["sudo", "cat", str(installed)] in cat_host.calls
    _assert_no_planted(again)


def test_an_unreadable_installed_file_with_no_sudo_is_a_failure(
    tmp_path: Path, overlay: Path
) -> None:
    """The failing control of the sudo read: with no sudo the permission error is the result, not a silent overwrite."""
    fs = tmp_path / "fs"
    reader, _calls = reader_planted()
    assert (
        handler(FakeHost(), fs, reader).handle(request(overlay, "nut-fixture")).changed
    )
    target = fs / "etc/nut/nut.conf"
    target.chmod(0)
    try:
        result = handler(FakeHost(), fs, reader).handle(
            request(overlay, "nut-fixture", mode="dry-run")
        )
    finally:
        target.chmod(0o640)
    assert result.status == "failure"
    assert "Permission denied" in result.stderr
