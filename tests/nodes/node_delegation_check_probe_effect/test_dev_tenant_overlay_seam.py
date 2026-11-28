"""The dev tenant credential reads the overlay the declared way, and names where it looked when none is served (OMN-26043)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_gateway import (
    DEV_TENANT_NODE,
    DevTenantCredential,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_proof_tenant_token_effect/overlay.example.yaml"
)


class _Reader:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.asked.append((folder, name))
        return SecretStr("placeholder-value")


def test_overlay_in_the_declared_place_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_dir = tmp_path / DEV_TENANT_NODE
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    reader = _Reader()
    credential = DevTenantCredential(reader=reader, secret_name="NAME_FROM_ROW")
    assert credential.token("cli-run-a") == "placeholder-value"
    assert reader.asked[0][1] == "NAME_FROM_ROW"


def test_absent_overlay_names_the_file_and_the_roots_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))
    with pytest.raises(CollectorAbsentError) as raised:
        DevTenantCredential(reader=_Reader(), secret_name="N").token("cli-run-a")
    text = str(raised.value)
    assert "NodeOverlayNotFoundError" in text
    assert f"{DEV_TENANT_NODE}/overlay.yaml" in text
    assert OVERLAY_ROOTS_ENV in text
