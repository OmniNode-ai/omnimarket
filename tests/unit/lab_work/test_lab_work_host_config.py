# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit node's host settings come from its contract (OMN-21623)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.contract_block_loader import ContractBlockError
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    MAX_TIMEOUT_SECONDS,
    ModelLabWorkUnitRequest,
    load_lab_work_unit_host_config,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit_host_config import (
    CONFIG_KEY,
    CONTRACT_PATH,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    LocalHostReader,
    LocalShellLabWorkExecutor,
    local_host_reader,
)

REMOTE_ENV = "ONEX_LAB_WORK_GIT_REMOTE_HOST"


def _block() -> dict[str, object]:
    document = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    block = document[CONFIG_KEY]
    assert isinstance(block, dict)
    return dict(block)


def _contract_with(tmp_path: Path, block: dict[str, object]) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({CONFIG_KEY: block}), encoding="utf-8")
    return path


def test_shipped_contract_carries_the_values_the_code_used_to_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REMOTE_ENV, raising=False)
    config = load_lab_work_unit_host_config()
    assert config.executable_search_path == (
        "~/.local/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    )
    assert config.max_unit_timeout_seconds == MAX_TIMEOUT_SECONDS == 7200
    assert config.git_timeout_seconds == 600
    assert config.git_remote_base == "https://github.com"
    assert config.login_cache_ttl_seconds == 300.0
    assert config.login_check_timeout_seconds == 20
    assert config.memory_probe_timeout_seconds == 10


def test_remote_base_is_set_by_the_overlay_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REMOTE_ENV, "mirror.internal")
    config = load_lab_work_unit_host_config()
    assert config.git_remote_host == "mirror.internal"
    assert config.git_remote_base == "https://mirror.internal"


@pytest.mark.parametrize("missing", sorted(_block()))
def test_a_block_missing_any_key_fails_to_load(tmp_path: Path, missing: str) -> None:
    block = _block()
    del block[missing]
    with pytest.raises(ContractBlockError, match=missing):
        load_lab_work_unit_host_config(_contract_with(tmp_path, block))


def test_an_unknown_key_fails_to_load(tmp_path: Path) -> None:
    block = {**_block(), "surprise": 1}
    with pytest.raises(ContractBlockError, match="surprise"):
        load_lab_work_unit_host_config(_contract_with(tmp_path, block))


def test_a_contract_with_no_block_fails_to_load(tmp_path: Path) -> None:
    path = tmp_path / "contract.yaml"
    path.write_text("name: x\n", encoding="utf-8")
    with pytest.raises(ContractBlockError, match=CONFIG_KEY):
        load_lab_work_unit_host_config(path)


def test_request_timeout_is_bounded_by_the_contract() -> None:
    fields = {
        "work_unit_id": "lw-0123456789abcdef",
        "target_host": "h202",
        "repo": "OmniNode-ai/demo",
        "commit_sha": "a" * 40,
        "argv": ["uv", "run", "pytest"],
        "lane": "lane-x",
    }
    ModelLabWorkUnitRequest(**fields, timeout_seconds=MAX_TIMEOUT_SECONDS)
    with pytest.raises(ValidationError):
        ModelLabWorkUnitRequest(**fields, timeout_seconds=MAX_TIMEOUT_SECONDS + 1)


def test_reader_reuses_the_login_reading_for_the_contract_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    block = {
        **_block(),
        "login_cache_ttl_seconds": 5.0,
        "login_check_timeout_seconds": 7,
    }
    config = load_lab_work_unit_host_config(_contract_with(tmp_path, block))
    seen: list[tuple[str, float]] = []

    def fake_login(path: str, timeout: float) -> bool:
        seen.append((path, timeout))
        return True

    monkeypatch.setattr(local_host_reader, "_claude_logged_in", fake_login)
    clock = [0.0]
    reader = LocalHostReader(now=lambda: clock[0], home=tmp_path, config=config)
    reader.read(["claude-login"])
    clock[0] += 4
    reader.read(["claude-login"])
    assert len(seen) == 1
    clock[0] += 2
    reader.read(["claude-login"])
    assert len(seen) == 2
    assert {timeout for _path, timeout in seen} == {7}
    assert seen[0][0].startswith(str(Path("~/.local/bin").expanduser()))


def test_runner_bounds_git_by_the_contract_and_fetches_from_its_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    block = {
        **_block(),
        "git_timeout_seconds": 11,
        "git_remote_scheme": "ssh",
        "git_remote_host": "elsewhere.internal",
    }
    config = load_lab_work_unit_host_config(_contract_with(tmp_path, block))
    runner = LocalShellLabWorkExecutor(tmp_path / "work", config=config)
    calls: list[dict[str, object]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner._git("status", env={})
    assert calls[0]["timeout"] == 11
    assert runner._url_base == "ssh://elsewhere.internal"
