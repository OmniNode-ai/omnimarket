# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_job_start_source_effect: the overlay holds every setting, and the CLI verbs (OMN-22897)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    NODE_NAME,
    ModelJobStartOverlay,
    ModelLedgerStore,
    load_overlay,
)
from omnimarket.nodes.node_job_start_source_effect.runtime_job_start_source import main
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_job_start_source_effect.support import (
    NODE_DIR,
    example_fields,
)

pytestmark = pytest.mark.unit

EXAMPLES = (
    "overlay.example.yaml",
    "overlay.ledger.example.yaml",
    "overlay.github.example.yaml",
)


@pytest.mark.parametrize("name", EXAMPLES)
def test_the_shipped_examples_load(name: str) -> None:
    loaded = ModelJobStartOverlay.model_validate(example_fields(name))
    assert loaded.settings.repositories


@pytest.mark.parametrize(
    "key",
    [
        "repositories",
        "window_seconds",
        "run_lookback_seconds",
        "max_age_seconds",
        "watched_runner_groups",
        "baseline_public_repositories",
    ],
)
def test_a_settings_key_with_no_default_refuses_when_absent(key: str) -> None:
    fields = example_fields()
    del fields["settings"][key]
    with pytest.raises(ValueError, match=key):
        ModelJobStartOverlay.model_validate(fields)


@pytest.mark.parametrize(
    "key",
    [
        "api_base",
        "token_env",
        "max_calls",
        "timeout_seconds",
        "page_size",
        "max_pages_per_repository",
    ],
)
def test_a_github_source_key_with_no_default_refuses_when_absent(key: str) -> None:
    fields = example_fields("overlay.github.example.yaml")
    del fields["source"][key]
    with pytest.raises(ValueError, match=key):
        ModelJobStartOverlay.model_validate(fields)


def test_an_empty_repository_list_refuses() -> None:
    fields = example_fields()
    fields["settings"]["repositories"] = []
    with pytest.raises(ValueError, match="repositories"):
        ModelJobStartOverlay.model_validate(fields)


def test_a_repository_listed_twice_refuses() -> None:
    fields = example_fields()
    repository = fields["settings"]["repositories"][0]
    fields["settings"]["repositories"] = [repository, repository]
    with pytest.raises(ValueError, match="twice"):
        ModelJobStartOverlay.model_validate(fields)


def test_the_overlay_never_holds_the_token_only_the_name_of_its_variable() -> None:
    text = (NODE_DIR / "overlay.github.example.yaml").read_text(encoding="utf-8")
    assert "token_env:" in text
    assert not re.search(r"(?i)\b(ghp_|github_pat_|bearer )", text)


def test_the_code_names_no_repository_group_or_organization() -> None:
    """No setting is hard-coded: the repositories, groups and organization come from the overlay."""
    offenders: list[str] = []
    for path in NODE_DIR.rglob("*.py"):
        text = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("# SPDX")
        )
        if re.search(r"OmniNode|omninode|omnibase|omnimarket/|omni-", text):
            offenders.append(path.name)
    assert offenders == []


# -- the CLI ---------------------------------------------------------------------------------


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fields: dict[str, Any]
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, fields)


def test_the_probe_verb_reads_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, tmp_path, example_fields("overlay.ledger.example.yaml"))
    monkeypatch.delenv("EXAMPLE_LEDGER_APPEND_COMMAND", raising=False)
    assert main(["probe"]) == 0
    out = capsys.readouterr().out
    assert "verdict=WORKING" in out
    assert "store=not-written" in out
    assert load_overlay() is not None


def test_the_read_verb_stores_in_memory_when_the_overlay_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, tmp_path, example_fields())
    assert main(["read"]) == 0
    assert "store=written" in capsys.readouterr().out


def test_the_read_verb_exits_one_when_the_read_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = example_fields()
    fields["source"]["fixture"]["starts"] = []
    fields["source"]["fixture"]["read_failure"] = "rate-limited"
    _install(monkeypatch, tmp_path, fields)
    assert main(["read"]) == 1
    out = capsys.readouterr().out
    assert "verdict=UNKNOWN" in out
    assert "failure=rate-limited" in out
    assert "starts=unread" in out


def test_a_not_working_reading_is_still_exit_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = example_fields()
    fields["source"]["fixture"]["starts"][0]["head_is_fork"] = True
    fields["source"]["fixture"]["starts"][0]["runner_group_name"] = fields["settings"][
        "watched_runner_groups"
    ][0]
    _install(monkeypatch, tmp_path, fields)
    assert main(["read"]) == 0
    assert "verdict=NOT_WORKING" in capsys.readouterr().out


def test_a_ledger_store_with_no_route_variable_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = example_fields("overlay.ledger.example.yaml")
    _install(monkeypatch, tmp_path, fields)
    store = ModelJobStartOverlay.model_validate(fields).store
    assert isinstance(store, ModelLedgerStore)
    monkeypatch.delenv(store.ledger_append.command_env, raising=False)
    monkeypatch.delenv(store.ledger_append.host_env, raising=False)
    assert main(["read"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_a_missing_overlay_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main(["probe"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    with pytest.raises(NodeOverlayError):
        load_overlay()


def test_the_node_directory_holds_the_overlay_examples_and_a_contract() -> None:
    for name in (*EXAMPLES, "contract.yaml", "metadata.yaml"):
        assert (NODE_DIR / name).is_file()
    assert yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())["name"] == NODE_NAME
