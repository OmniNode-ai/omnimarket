# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared public key install: idempotence and overlay-only key text."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers.handler_lab_host_public_keys_install import (
    NODE_OVERLAY_NAME,
    HandlerLabHostPublicKeysInstall,
    resolve_declared_keys,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.models import (
    EnumInstallStatus,
    EnumKeyFile,
    EnumLineState,
    ModelDeclaredKeysInstallRequest,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols import (
    HostTransportError,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.runtime_lab_host_public_keys_install import (
    main,
)

pytestmark = pytest.mark.unit


def _key(name: str) -> str:
    encoded = base64.b64encode(f"{name}-declared".encode()).decode()
    return f"ssh-ed25519 {encoded} {name}"


class FakeTransport:
    def __init__(self) -> None:
        self.files: dict[str, list[str]] = {}
        self.writes = 0
        self.refuse: set[str] = set()

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
        if host.name in self.refuse:
            raise HostTransportError("rc=255: Permission denied (publickey)")
        lines = self.files.setdefault(host.name, [])
        if any(needle in existing for existing in lines):
            return EnumLineState.PRESENT
        if not apply:
            return EnumLineState.ABSENT
        lines.append(line)
        self.writes += 1
        return EnumLineState.ADDED


@pytest.fixture
def table() -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(name="alpha", target="alpha.example.invalid"),
            ModelLabHostRow(name="beta", target="beta.example.invalid"),
            ModelLabHostRow(
                name="gamma",
                target="gamma.example.invalid",
                available=False,
                unavailable_reason="down",
            ),
            ModelLabHostRow(
                name="delta",
                target="delta.example.invalid",
                contributor=True,
                lane_binding="other",
                lane_not_ready_reason="x",
            ),
        )
    )


def test_lab_host_public_keys_idempotent(table: ModelLabHostsTable) -> None:
    fake = FakeTransport()
    keys = (_key("one"), _key("two"))
    handler = HandlerLabHostPublicKeysInstall(fake)
    request = ModelDeclaredKeysInstallRequest(table=table, public_keys=keys)
    first = handler.handle(request)
    assert first.exit_code == 0
    by_host = {r.host: r for r in first.receipts}
    assert [r.host for r in first.receipts] == ["alpha", "beta", "gamma", "delta"]
    for name in ("alpha", "beta"):
        assert by_host[name].status == EnumInstallStatus.OK
        assert by_host[name].keys_added == 2
        assert fake.files[name] == list(keys)
    assert by_host["gamma"].status == EnumInstallStatus.SKIPPED
    assert by_host["delta"].status == EnumInstallStatus.SKIPPED
    before = {h: list(v) for h, v in fake.files.items()}
    writes = fake.writes
    second = handler.handle(request)
    assert second.exit_code == 0
    assert fake.files == before
    assert fake.writes == writes
    assert all(r.keys_added == 0 for r in second.receipts)
    assert {r.host: r.keys_present for r in second.receipts}["alpha"] == 2


def test_dry_run_writes_nothing_and_refusal_is_per_host(
    table: ModelLabHostsTable,
) -> None:
    fake = FakeTransport()
    fake.refuse.add("beta")
    handler = HandlerLabHostPublicKeysInstall(fake)
    planned = handler.handle(
        ModelDeclaredKeysInstallRequest(
            table=table, public_keys=(_key("one"),), dry_run=True, hosts=("alpha",)
        )
    )
    assert planned.receipts[0].status == EnumInstallStatus.PLANNED
    assert fake.writes == 0
    result = handler.handle(
        ModelDeclaredKeysInstallRequest(table=table, public_keys=(_key("one"),))
    )
    assert result.exit_code == 1
    assert {r.host: r.status for r in result.receipts}["beta"] == (
        EnumInstallStatus.REFUSED
    )
    assert {r.host: r.status for r in result.receipts}["alpha"] == (
        EnumInstallStatus.OK
    )


def test_lab_host_public_keys_overlay_only(
    table: ModelLabHostsTable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No explicit keys and no overlay: refuse naming the overlay, no packaged fallback.
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match="declared_public_keys"):
        resolve_declared_keys()
    refused = HandlerLabHostPublicKeysInstall(FakeTransport()).handle(
        ModelDeclaredKeysInstallRequest(table=table)
    )
    assert refused.exit_code == 2
    assert refused.refusal is not None
    # The overlay supplies the keys.
    overlay = tmp_path / NODE_OVERLAY_NAME
    overlay.mkdir()
    keys = (_key("one"), _key("two"))
    (overlay / "overlay.yaml").write_text(
        "declared_public_keys: |\n" + "".join(f"  {k}\n" for k in keys),
        encoding="utf-8",
    )
    assert resolve_declared_keys() == keys
    fake = FakeTransport()
    result = HandlerLabHostPublicKeysInstall(fake).handle(
        ModelDeclaredKeysInstallRequest(table=table)
    )
    assert result.exit_code == 0
    assert fake.files["alpha"] == list(keys)
    # A malformed declared key refuses before any write.
    (overlay / "overlay.yaml").write_text(
        "declared_public_keys: not-a-key\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid public key line"):
        resolve_declared_keys()
    # No public file of the node or this test holds key text or a host address.
    node_dir = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_lab_host_key_distribution_effect"
    )
    key_text = re.compile(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-\w+)\s+AAAA")
    address = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    files = [*node_dir.rglob("*.py"), *node_dir.rglob("*.yaml"), Path(__file__)]
    assert len(files) > 5
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not key_text.search(text), path.name
        assert not address.search(text), path.name


def test_cli_refuses_without_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    assert main(["--dry-run"]) == 2
    assert "refused" in capsys.readouterr().err
