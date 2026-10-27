# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.4 pinned reads cannot escape the manifest root (OMN-21127 AC6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)

from omnimarket.nodes.node_context_source_ledger_effect import (
    NodeContextSourceLedgerEffect,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    LedgerSourceReadError,
    MarkdownLedgerRowReader,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[4]
CONTRACT = ROOT / "src/omnimarket/nodes/node_context_source_ledger_effect/contract.yaml"
ROW = '2026-10-02T11:25:17Z | RULING | lane=proof | ticket=OMN-21127 | kind=decision | "keep every word" | evidence'
AS_OF = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "escape", ["absolute", "parent", "file_symlink", "directory_symlink"]
)
def test_manifest_root_rejects_escape_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    root = tmp_path / "manifest"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(ROW)
    if escape == "absolute":
        path = outside
    elif escape == "parent":
        path = Path("../outside.md")
    elif escape == "file_symlink":
        path = Path("linked.md")
        (root / path).symlink_to(outside)
    else:
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
        path = Path("linked/outside.md")
    reads: list[Path] = []
    original = Path.read_text

    def observed(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observed)
    with pytest.raises(LedgerSourceReadError):
        MarkdownLedgerRowReader([path], manifest_root=root).read_rows(as_of=AS_OF)
    assert outside not in reads


def test_manifest_root_rechecks_replaced_path(tmp_path: Path) -> None:
    root = tmp_path / "manifest"
    root.mkdir()
    path = root / "ledger.md"
    path.write_text(ROW)
    reader = MarkdownLedgerRowReader([path], manifest_root=root)
    outside = tmp_path / "outside.md"
    outside.write_text(ROW)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(LedgerSourceReadError):
        reader.read_rows(as_of=AS_OF)


def test_pinned_overlay_selects_reader_and_preserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "manifest"
    (root / "archive").mkdir(parents=True)
    (root / "archive/day.md").write_text(ROW + "\n")
    contract = yaml.safe_load(CONTRACT.read_text())
    overlay = yaml.safe_load(
        (Path(__file__).parent / "fixtures/pinned-overlay.yaml").read_text()
    )
    contract["context_source_ledger"]["ledger_source"].update(
        overlay["context_source_ledger"]["ledger_source"]
    )
    configured = tmp_path / "contract.yaml"
    configured.write_text(yaml.safe_dump(contract))
    monkeypatch.setenv("ONEX_LEDGER_MANIFEST_ROOT", str(root))
    node = NodeContextSourceLedgerEffect(contract_path=configured)
    request = ModelContextSourceRequest(
        factor=EnumContextFactor.OPERATOR_RULINGS,
        access=ModelContextAccess(tenant_id="fixture"),
        revision="2026-10-02T12:00:00Z",
    )
    result = node.handle(request)
    assert result.status.value == "resolved"
    assert [entry.text for entry in result.entries] == [ROW]
    assert (
        node.handle(
            request.model_copy(update={"access": ModelContextAccess(tenant_id="other")})
        ).status.value
        == "denied"
    )
    (root / "archive/day.md").unlink()
    assert node.handle(request).status.value == "unavailable"


@pytest.mark.parametrize(
    "fields", [{"manifest_root": None}, {"manifest_root": ""}, {"ledger_paths": []}]
)
def test_pinned_binding_requires_explicit_root_and_paths(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    contract["context_source_ledger"]["ledger_source"].update(
        mode="pinned", manifest_root=str(tmp_path), ledger_paths=["ledger.md"]
    )
    contract["context_source_ledger"]["ledger_source"].update(fields)
    configured = tmp_path / "contract.yaml"
    configured.write_text(yaml.safe_dump(contract))
    with pytest.raises(ValueError, match=r"manifest_root|ledger_paths"):
        NodeContextSourceLedgerEffect(contract_path=configured)


def test_pinned_reader_requires_manifest_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(ROW)
    with pytest.raises(TypeError, match="manifest_root"):
        MarkdownLedgerRowReader([outside])
