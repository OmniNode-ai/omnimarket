# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models refresh-prices``: the explicit read of the model price list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.handlers import model_catalog
from omnimarket.nodes.node_model_setup_effect.cli_catalog import (
    refresh_prices_command,
)

pytestmark = pytest.mark.unit

_DOCUMENT = {
    "openrouter": {"models": {"vendor/m": {"id": "vendor/m", "cost": {"input": 1}}}}
}


def _serve(
    monkeypatch: pytest.MonkeyPatch, home: Path, body: bytes | None
) -> list[str]:
    """The refresh over a recorded transport and an isolated ONEX home."""
    reads: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        reads.append(url)
        if body is None:
            raise OSError("models.dev unreachable in this test")
        return body

    monkeypatch.setattr(
        model_catalog,
        "refresh_models_dev",
        lambda _onex_home=None: model_catalog.load_models_dev(
            home, fetch=fetch, force=True
        ),
    )
    return reads


def test_the_command_reads_models_dev_and_keeps_the_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _serve(monkeypatch, tmp_path, json.dumps(_DOCUMENT).encode())
    result = CliRunner().invoke(refresh_prices_command, ["--json"])
    assert result.exit_code == 0, result.output
    assert len(reads) == 1
    report = json.loads(result.output)
    assert report["models"] == 1
    assert report["error"] is None
    kept = tmp_path / str(model_catalog.catalog_config()["models_dev"]["cache_file"])
    assert json.loads(kept.read_text(encoding="utf-8")) == _DOCUMENT
    # A session now prices from that copy, with no read of its own.
    monkeypatch.setattr(model_catalog, "_onex_home", lambda: tmp_path)
    index = model_catalog.kept_models_dev_index()
    assert ("openrouter", "vendor/m") in index.models


def test_the_command_reads_again_even_when_the_copy_is_young(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _serve(monkeypatch, tmp_path, json.dumps(_DOCUMENT).encode())
    for _ in range(2):
        assert CliRunner().invoke(refresh_prices_command, []).exit_code == 0
    assert len(reads) == 2


def test_a_failed_read_exits_one_and_keeps_the_earlier_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, tmp_path, json.dumps(_DOCUMENT).encode())
    assert CliRunner().invoke(refresh_prices_command, []).exit_code == 0
    _serve(monkeypatch, tmp_path, None)
    result = CliRunner().invoke(refresh_prices_command, [])
    assert result.exit_code == 1
    assert "earlier copy" in result.output
    kept = tmp_path / str(model_catalog.catalog_config()["models_dev"]["cache_file"])
    assert json.loads(kept.read_text(encoding="utf-8")) == _DOCUMENT
