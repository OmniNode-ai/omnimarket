# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two local servers can run the same model, and each is its own model (OMN-23413).

A lab with two GPU hosts serves one model from both (vLLM on one, llama.cpp on the
other). ``onex models add local`` already declared each as its own backend, but
everything after it named a local model by its name alone. Failure modes, each
caught by a test below:

  FM2  ``models list --json`` kept one URL per model name, so one server vanished.
  FM10 each server's model showed the provider's one test, not its own.
  FM3  ``models first local M`` with M on two servers was refused by the local
       reorder (both copies dropped) or put the wrong server first.
  FM4  no way to choose the second server: the first-choice route took the first
       local route with that name.
  FM5  ``models remove local --model M`` removed M from both servers.
  FM6  no way to remove M from one server only.
  FM7  ``models reorder local M M`` mapped both names to the first server's entry,
       declaring it twice and dropping the other.
  FM9  positive control: with one server per name, nothing needs a URL.

The servers are real HTTP servers on 127.0.0.1; the pinned test delegation is the
stub the OMN-21211 tests use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.handlers.delegation_first_choice import (
    first_choice_path,
    first_choice_route,
)
from tests.unit.cli.test_model_setup_local_url_omn21162 import (
    _MODEL,
    _OpenAICompatible,
    _serve,
    server,
    state,
)
from tests.unit.cli.test_multiple_local_models_omn21211 import _ModelPerBackend, pinned

pytestmark = pytest.mark.unit

# The fixtures this module shares with the OMN-21162 and OMN-21211 tests.
__all__ = ["pinned", "server", "state"]

_OTHER = "llama-3.3-8b"


@pytest.fixture
def second() -> Iterator[str]:
    """A second model server: the other GPU host, serving the same model."""
    yield from _serve(_OpenAICompatible)


def _models(*args: str) -> Any:
    return CliRunner().invoke(models_group, list(args))


def _backends(state_dir: Path) -> list[tuple[str, str, str]]:
    data = yaml.safe_load((state_dir / "bifrost_overrides.yaml").read_text("utf-8"))
    return [
        (b["backend_id"], b["model_name"], b["endpoint_url"])
        for b in data.get("backends") or []
    ]


def _local_row() -> dict[str, Any]:
    rows = (json.loads(line) for line in _models("list", "--json").output.splitlines())
    return next(row for row in rows if row["provider"] == "local")


def _two_servers(first: str, second: str) -> None:
    for url, model in ((first, _MODEL), (first, _OTHER), (second, _MODEL)):
        done = _models("add", "local", "--url", url, "--model", model)
        assert done.exit_code == 0, done.output


def test_status_lists_each_server_of_a_shared_model_with_its_own_test(
    server: str, second: str, pinned: _ModelPerBackend, state: Path
) -> None:
    """FM2, FM10."""
    _two_servers(server, second)
    row = _local_row()
    assert row["models"] == [_MODEL, _OTHER, _MODEL]
    assert [
        (m["model"], m["base_url"], m["backend_id"], m["last_test"]["status"])
        for m in row["local_models"]
    ] == [
        (_MODEL, server, "local-url", "passed"),
        (_OTHER, server, "local-url-2", "passed"),
        (_MODEL, second, "local-url-3", "passed"),
    ]
    # Each server's test is its own: the receipt was pinned to that backend.
    assert [m["last_test"]["backend_id"] for m in row["local_models"]] == [
        "local-url",
        "local-url-2",
        "local-url-3",
    ]
    assert "endpoints" not in row  # the lossy model -> URL view is gone
    listed = _models("list").output
    assert f"{_MODEL}@{server}" in listed
    assert f"{_MODEL}@{second}" in listed


def test_first_needs_a_url_when_two_servers_run_the_model(
    server: str, second: str, pinned: _ModelPerBackend, state: Path
) -> None:
    """FM3: refused, both URLs named, nothing changed."""
    _two_servers(server, second)
    before = _backends(state)
    refused = _models("first", "local", _MODEL)
    assert refused.exit_code != 0
    assert server in refused.output
    assert second in refused.output
    assert "--url" in refused.output
    assert _backends(state) == before
    assert not first_choice_path().exists()


def test_first_with_a_url_puts_that_server_first_and_routes_to_it(
    server: str,
    second: str,
    pinned: _ModelPerBackend,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FM4: the second server is chosen, moved to the front, and routed first."""
    _two_servers(server, second)
    # The run resolves local routes from the overlay the add wrote.
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(state / "bifrost_overrides.yaml"))
    done = _models("first", "local", _MODEL, "--url", second)
    assert done.exit_code == 0, done.output
    assert second in done.output
    assert [b[0] for b in _backends(state)] == [
        "local-url-3",
        "local-url",
        "local-url-2",
    ]
    route = first_choice_route("research")
    assert route is not None
    assert route.endpoint_ref == f"{second}/v1/chat/completions"
    row = _local_row()
    assert row["tried_first"] is True
    assert (row["first_model"], row["first_url"]) == (_MODEL, second)
    # The other server is still declared: nothing was dropped.
    assert sorted(b[2] for b in _backends(state)) == sorted(
        [
            f"{server}/v1/chat/completions",
            f"{server}/v1/chat/completions",
            f"{second}/v1/chat/completions",
        ]
    )


def test_a_url_is_refused_for_a_provider_that_is_not_local(state: Path) -> None:
    refused = _models("first", "openrouter", "x", "--url", "http://127.0.0.1:9")
    assert refused.exit_code != 0
    assert "No such option" not in refused.output  # the option exists
    assert "--url names a local model's server" in refused.output


def test_remove_takes_a_shared_model_off_one_server_only(
    server: str, second: str, pinned: _ModelPerBackend, state: Path
) -> None:
    """FM5 refused without a URL; FM6 removes only the named server's entry."""
    _two_servers(server, second)
    before = _backends(state)
    refused = _models("remove", "local", "--model", _MODEL)
    assert refused.exit_code != 0
    assert server in refused.output
    assert second in refused.output
    assert _backends(state) == before
    done = _models("remove", "local", "--model", _MODEL, "--url", server)
    assert done.exit_code == 0, done.output
    assert _backends(state) == [
        ("local-url-2", _OTHER, f"{server}/v1/chat/completions"),
        ("local-url-3", _MODEL, f"{second}/v1/chat/completions"),
    ]


def test_reorder_names_each_server_of_a_shared_model(
    server: str, second: str, pinned: _ModelPerBackend, state: Path
) -> None:
    """FM7: a bare shared name is refused; MODEL@URL places each server."""
    _two_servers(server, second)
    before = _backends(state)
    refused = _models("reorder", "local", _MODEL, _OTHER, _MODEL)
    assert refused.exit_code != 0
    assert f"{_MODEL}@{server}" in refused.output
    assert _backends(state) == before
    done = _models(
        "reorder", "local", f"{_MODEL}@{second}", _OTHER, f"{_MODEL}@{server}"
    )
    assert done.exit_code == 0, done.output
    assert [b[0] for b in _backends(state)] == [
        "local-url-3",
        "local-url-2",
        "local-url",
    ]


def test_one_server_per_model_needs_no_url(
    server: str, pinned: _ModelPerBackend, state: Path
) -> None:
    """FM9, the positive control: names alone still work when they are unique."""
    for model in (_MODEL, _OTHER):
        assert _models("add", "local", "--url", server, "--model", model).exit_code == 0
    assert _models("reorder", "local", _OTHER, _MODEL).exit_code == 0
    first = _models("first", "local", _MODEL)
    assert first.exit_code == 0, first.output
    assert _local_row()["first_url"] == server
    assert _models("remove", "local", "--model", _OTHER).exit_code == 0
    assert [b[1] for b in _backends(state)] == [_MODEL]
    assert f"[models: {_MODEL}]" in _models("list").output
