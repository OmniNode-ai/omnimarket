# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installing the declared public key set on every pool host, read from an overlay."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes import node_lab_host_key_distribution_effect as node_package
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers.handler_lab_host_public_keys_install import (
    HandlerLabHostPublicKeysInstall,
    resolve_declared_keys,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.models import (
    EnumHostInstallStatus,
    EnumKeyFile,
    EnumLineState,
    ModelHostKeyMaterial,
    ModelLabHostKeyDistributionRequest,
    ModelLabHostPublicKeysInstallRequest,
    ModelLabHostPublicKeysOverlay,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols import (
    HostTransportError,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.runtime_lab_host_key_distribution import (
    main,
)

pytestmark = pytest.mark.unit

HOSTS = ("alpha", "beta", "gamma", "delta")


def _key(name: str) -> str:
    encoded = base64.b64encode(f"{name}-declared".encode()).decode()
    return f"ssh-ed25519 {encoded} {name}"


KEYS = tuple(_key(name) for name in ("one", "two", "three"))


class FakeTransport:
    def __init__(self, table: ModelLabHostsTable) -> None:
        self.files: dict[str, list[str]] = {row.name: [] for row in table.hosts}
        self.touched: list[str] = []
        self.refuse: set[str] = set()

    def read_material(
        self, host: ModelLabHostRow, request: ModelLabHostKeyDistributionRequest
    ) -> ModelHostKeyMaterial:
        raise AssertionError("the installer reads no host material")

    def ensure_line(
        self,
        host: ModelLabHostRow,
        file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState:
        assert file == EnumKeyFile.AUTHORIZED_KEYS
        self.touched.append(host.name)
        if host.name in self.refuse:
            raise HostTransportError("Permission denied (publickey)")
        if any(needle in existing for existing in self.files[host.name]):
            return EnumLineState.PRESENT
        if not apply:
            return EnumLineState.ABSENT
        self.files[host.name].append(line)
        return EnumLineState.ADDED


@pytest.fixture
def table() -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=tuple(
            ModelLabHostRow(
                name=name,
                target=f"{name}.example.invalid",
                available=name != "delta",
                unavailable_reason="off the pool" if name == "delta" else None,
            )
            for name in HOSTS
        )
    )


@pytest.fixture
def request_model(table: ModelLabHostsTable) -> ModelLabHostPublicKeysInstallRequest:
    return ModelLabHostPublicKeysInstallRequest(
        table=table, keys=ModelLabHostPublicKeysOverlay(keys=KEYS)
    )


def test_lab_host_public_keys_idempotent(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    fake = FakeTransport(table)
    handler = HandlerLabHostPublicKeysInstall(fake)
    first = handler.handle(request_model)
    assert first.exit_code == 0
    assert [(r.host, r.status) for r in first.hosts] == [
        ("alpha", EnumHostInstallStatus.OK),
        ("beta", EnumHostInstallStatus.OK),
        ("gamma", EnumHostInstallStatus.OK),
        ("delta", EnumHostInstallStatus.SKIPPED),
    ]
    for name in ("alpha", "beta", "gamma"):
        assert fake.files[name] == list(KEYS)
    assert fake.files["delta"] == []
    assert "delta" not in fake.touched
    assert all(r.keys_added == 3 for r in first.hosts[:3])

    before = {name: list(lines) for name, lines in fake.files.items()}
    second = handler.handle(request_model)
    assert second.exit_code == 0
    assert fake.files == before
    assert all(r.keys_added == 0 and r.keys_present == 3 for r in second.hosts[:3])


def test_lab_host_public_keys_idempotent_keeps_foreign_lines(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    fake = FakeTransport(table)
    fake.files["alpha"].append(KEYS[1] + " with a different comment")
    result = HandlerLabHostPublicKeysInstall(fake).handle(request_model)
    assert result.hosts[0].keys_added == 2
    assert result.hosts[0].keys_present == 1
    assert fake.files["alpha"][0] == KEYS[1] + " with a different comment"


def test_lab_host_public_keys_dry_run_writes_nothing(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    fake = FakeTransport(table)
    result = HandlerLabHostPublicKeysInstall(fake).handle(
        request_model.model_copy(update={"dry_run": True})
    )
    assert result.exit_code == 0
    assert all(f == [] for f in fake.files.values())
    assert [r.status for r in result.hosts[:3]] == [EnumHostInstallStatus.PLANNED] * 3
    assert all(line.split()[2].startswith("would-add=") for line in result.lines()[:3])


def test_lab_host_public_keys_one_unreachable_host_is_refused_the_rest_ok(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    fake = FakeTransport(table)
    fake.refuse.add("beta")
    result = HandlerLabHostPublicKeysInstall(fake).handle(request_model)
    assert result.exit_code == 1
    statuses = {r.host: r.status for r in result.hosts}
    assert statuses["beta"] == EnumHostInstallStatus.REFUSED
    assert statuses["alpha"] == statuses["gamma"] == EnumHostInstallStatus.OK
    assert "Permission denied" in result.hosts[1].detail


def test_lab_host_public_keys_named_scope_and_unknown_host(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    fake = FakeTransport(table)
    handler = HandlerLabHostPublicKeysInstall(fake)
    scoped = handler.handle(request_model.model_copy(update={"hosts": ("gamma",)}))
    assert [r.host for r in scoped.hosts] == ["gamma"]
    assert set(fake.touched) == {"gamma"}
    unknown = handler.handle(request_model.model_copy(update={"hosts": ("nowhere",)}))
    assert unknown.exit_code == 2
    assert unknown.refusal
    assert "nowhere" in unknown.refusal


def test_lab_host_public_keys_result_lines_hold_no_key_text(
    table: ModelLabHostsTable, request_model: ModelLabHostPublicKeysInstallRequest
) -> None:
    result = HandlerLabHostPublicKeysInstall(FakeTransport(table)).handle(request_model)
    text = "\n".join(result.lines()) + result.model_dump_json()
    for key in KEYS:
        assert key.split()[1] not in text
    assert "example.invalid" not in text


def _write_overlay(root: Path, keys: tuple[str, ...]) -> None:
    folder = root / "lab_host_public_keys"
    folder.mkdir(parents=True)
    (folder / "overlay.yaml").write_text(
        yaml.safe_dump({"keys": list(keys)}), encoding="utf-8"
    )


def test_lab_host_public_keys_overlay_only(
    tmp_path: Path, table: ModelLabHostsTable
) -> None:
    # The key set comes from the overlay under the roots variable and from nowhere else.
    root = tmp_path / "overlays"
    _write_overlay(root, KEYS)
    env = {OVERLAY_ROOTS_ENV: str(root)}
    assert resolve_declared_keys(env).keys == KEYS
    fake = FakeTransport(table)
    result = HandlerLabHostPublicKeysInstall(
        fake, table_source=lambda: table, keys_source=lambda: resolve_declared_keys(env)
    ).handle(ModelLabHostPublicKeysInstallRequest())
    assert result.exit_code == 0
    assert fake.files["alpha"] == list(KEYS)

    # With no overlay the node refuses by name and installs nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NodeOverlayError, match="lab_host_public_keys"):
        resolve_declared_keys({OVERLAY_ROOTS_ENV: str(empty)})
    bare = FakeTransport(table)
    refused = HandlerLabHostPublicKeysInstall(
        bare,
        table_source=lambda: table,
        keys_source=lambda: resolve_declared_keys({OVERLAY_ROOTS_ENV: str(empty)}),
    ).handle(ModelLabHostPublicKeysInstallRequest())
    assert refused.exit_code == 2
    assert refused.refusal
    assert "lab_host_public_keys" in refused.refusal
    assert bare.touched == []


def test_lab_host_public_keys_overlay_only_refuses_a_malformed_overlay(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlays"
    _write_overlay(root, ("not a public key",))
    with pytest.raises(NodeOverlayError) as raised:
        resolve_declared_keys({OVERLAY_ROOTS_ENV: str(root)})
    assert "not a public key" not in str(raised.value)
    with pytest.raises(ValueError, match="repeat"):
        ModelLabHostPublicKeysOverlay(keys=(KEYS[0], KEYS[0] + " again"))


_KEY_TEXT = re.compile(
    r"(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-[a-z0-9]+|sk-[a-z0-9@.-]+)\s+AAAA[A-Za-z0-9+/]{16,}"
)
_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def test_lab_host_public_keys_overlay_only_no_public_file_holds_keys_or_addresses() -> (
    None
):
    package = Path(node_package.__file__).parent
    scanned = [
        p
        for p in package.rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".md"}
    ]
    assert any(p.name == "handler_lab_host_public_keys_install.py" for p in scanned)
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        assert _KEY_TEXT.search(text) is None, path.name
        assert _ADDRESS.search(text) is None, path.name


def test_lab_host_public_keys_cli_declared_keys_refuses_without_overlay(
    tmp_path: Path,
    table: ModelLabHostsTable,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_file = tmp_path / "table.yaml"
    table_file.write_text(table.model_dump_json(), encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(empty))
    assert main(["--declared-keys", "--table", str(table_file), "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "lab-host-public-keys-install refused" in err
    assert "lab_host_public_keys" in err
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_declared_keys_refuses_allow_unavailable() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--declared-keys", "--allow-unavailable", "delta"])
    assert exc.value.code == 2
