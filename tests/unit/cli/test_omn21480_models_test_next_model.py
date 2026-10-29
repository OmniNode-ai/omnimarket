# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models test`` tries the next stored model on an upstream 429 (OMN-21480).

The clean-VM walk of 2026-10-28 stored ``google/gemma-4-31b-it:free`` then
``cohere/north-mini-code:free``; the first answered 429, the second answered, and
the provider was still reported unusable. The test delegation is a stub that
prints what ``onex delegate --json`` prints. No network is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    local_byok_key_test_failed,
)
from omnimarket.routing import byok_model_discovery

pytestmark = pytest.mark.unit

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_KEY = "sk-or-v1-synthetic-omn21480-key-never-printed"
_FIRST = "google/gemma-4-31b-it:free"
_SECOND = "cohere/north-mini-code:free"
_429 = f"{_FIRST} is temporarily rate-limited upstream. Please retry shortly"


def _throttled(_model: str | None) -> tuple[int, str, str]:
    message = f"Client error '429 Too Many Requests' | provider response: {_429}"
    result = {
        "attempts": [
            {
                "backend_id": "byok-openrouter",
                "failure_class": "rate_limited",
                "error_message": message,
            }
        ]
    }
    return (
        1,
        json.dumps({"result": result}) + "\n",
        f"onex delegate failed: pinned_backend_failed: {message}\n",
    )


def _answered(tmp_path: Path, model: str) -> tuple[int, str, str]:
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {"status": "success", "backend_id": "byok-openrouter", "model": model}
        )
    )
    return (0, "", f"receipt: {path.as_posix()}")


def _stub(tmp_path: Path, throttled: set[str], calls: list[str]) -> Any:
    def run(backend_id: str, model: str | None = None) -> tuple[int, str, str]:
        asked = model or _FIRST
        calls.append(asked)
        return _throttled(asked) if asked in throttled else _answered(tmp_path, asked)

    return run


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        byok_model_discovery,
        "get_models_json",
        lambda **_: {"data": [{"id": _FIRST}, {"id": _SECOND}]},
    )


def _add_two(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", _stub(tmp_path, set(), []))
    added = CliRunner().invoke(
        models_group,
        ["add", "openrouter", "--model", _FIRST, "--model", _SECOND],
        input=_KEY,
    )
    assert added.exit_code == 0, added.output


def _test_json(monkeypatch: pytest.MonkeyPatch, run: Any) -> dict[str, Any]:
    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", run)
    result = CliRunner().invoke(models_group, ["test", "openrouter", "--json"])
    shown: dict[str, Any] = json.loads(result.output.strip().splitlines()[-1])
    return shown


def test_ac1_a_429_on_the_first_model_tries_the_next_and_the_provider_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_two(monkeypatch, tmp_path)
    calls: list[str] = []
    shown = _test_json(monkeypatch, _stub(tmp_path, {_FIRST}, calls))
    assert calls == [_FIRST, _SECOND]
    assert shown["status"] == "passed", shown
    assert shown["model"] == _SECOND
    assert local_byok_key_test_failed("openrouter") is False
    listed = CliRunner().invoke(models_group, ["list"]).output
    assert "last test passed" in listed
    assert "Delegation does not use this key" not in listed


def test_ac1_the_second_model_is_not_tried_when_the_first_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_two(monkeypatch, tmp_path)
    calls: list[str] = []
    shown = _test_json(monkeypatch, _stub(tmp_path, set(), calls))
    assert calls == [_FIRST]
    assert shown["status"] == "passed"
    assert shown["model"] == _FIRST


def test_ac2_every_stored_model_throttled_is_throttled_not_a_failed_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_two(monkeypatch, tmp_path)
    calls: list[str] = []
    shown = _test_json(monkeypatch, _stub(tmp_path, {_FIRST, _SECOND}, calls))
    assert calls == [_FIRST, _SECOND]
    assert shown["status"] == "throttled", shown
    assert shown["status"] != "failed"
    assert "every stored model is throttled upstream (HTTP 429)" in shown["reason"]
    assert _FIRST in shown["reason"]
    assert _SECOND in shown["reason"]
    assert local_byok_key_test_failed("openrouter") is False
    assert "FAILED" not in CliRunner().invoke(models_group, ["list"]).output
