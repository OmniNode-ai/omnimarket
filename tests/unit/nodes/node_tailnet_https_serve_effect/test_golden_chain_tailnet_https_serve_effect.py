# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: an install serves the upstream and reads it back, a status reads it, an uninstall takes it away."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_tailnet_https_serve_effect import (
    HandlerTailnetHttpsServe,
    NodeTailnetHttpsServeEffect,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.container.container_tailnet_https_serve_effect import (
    create_handler_tailnet_https_serve,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    EnumServeOutcome,
    ModelServeOverlay,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.runtime_tailnet_https_serve import (
    main,
)
from tests.unit.nodes.node_tailnet_https_serve_effect.support import (
    ENV_FILE,
    FOREIGN,
    OTHER_PORT,
    PORT,
    UPSTREAM,
    BreakableBackend,
    build,
    request,
)

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[4]
_NODE = _ROOT / "src/omnimarket/nodes/node_tailnet_https_serve_effect"


def test_contract_models_handler_metadata_and_entry_point() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    metadata = yaml.safe_load((_NODE / "metadata.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"] == {"external_trigger": True}
    for key in ("input_model", "output_model"):
        module, name = contract[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module), name)
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerTailnetHttpsServe
    )
    assert issubclass(NodeTailnetHttpsServeEffect, HandlerTailnetHttpsServe)
    assert metadata["capabilities"]["side_effect_class"] == "write"
    entries = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        entries["node_tailnet_https_serve_effect"]
        == "omnimarket.nodes.node_tailnet_https_serve_effect"
    )


def test_example_overlay_validates_and_runs_on_the_in_memory_backend() -> None:
    overlay = ModelServeOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text())
    )
    assert overlay.backend == "in-memory"
    handler = create_handler_tailnet_https_serve(overlay)
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.INSTALLED
    assert result.https_port == overlay.listener.https_port


def test_install_writes_the_env_serves_and_reads_back_and_reports_once() -> None:
    handler, probe, backend, reporter = build()
    result = handler.handle(request(EnumServeMode.INSTALL))
    assert result.outcome is EnumServeOutcome.INSTALLED
    assert result.ok
    assert result.changed
    assert result.served_target == UPSTREAM
    assert probe.probed == [f"{UPSTREAM}/health"]
    assert backend.served == {PORT: UPSTREAM}
    env = backend.env_files[ENV_FILE]
    assert f"EXAMPLE_HTTPS_PORT={PORT}\n" in env
    assert f"EXAMPLE_UPSTREAM={UPSTREAM}\n" in env
    assert reporter.reports == [result]


def test_a_second_install_changes_nothing_and_still_reads_back() -> None:
    handler, _, backend, reporter = build()
    handler.handle(request(EnumServeMode.INSTALL))
    served_before = backend.calls.count(f"serve {PORT} {UPSTREAM}")
    again = handler.handle(request(EnumServeMode.INSTALL))
    assert again.outcome is EnumServeOutcome.ALREADY_INSTALLED
    assert again.changed is False
    assert backend.calls.count(f"serve {PORT} {UPSTREAM}") == served_before
    assert len(reporter.reports) == 2


def test_status_reads_serving_after_install_and_not_serving_after_uninstall() -> None:
    handler, _, _, _ = build()
    assert (
        handler.handle(request(EnumServeMode.STATUS)).outcome
        is EnumServeOutcome.NOT_SERVING
    )
    handler.handle(request(EnumServeMode.INSTALL))
    assert (
        handler.handle(request(EnumServeMode.STATUS)).outcome
        is EnumServeOutcome.SERVING
    )
    handler.handle(request(EnumServeMode.UNINSTALL))
    assert (
        handler.handle(request(EnumServeMode.STATUS)).outcome
        is EnumServeOutcome.NOT_SERVING
    )


def test_uninstall_is_the_rollback_and_leaves_the_other_listener_untouched() -> None:
    handler, _, backend, _ = build(backend=BreakableBackend({OTHER_PORT: FOREIGN}))
    handler.handle(request(EnumServeMode.INSTALL))
    assert backend.served == {PORT: UPSTREAM, OTHER_PORT: FOREIGN}
    result = handler.handle(request(EnumServeMode.UNINSTALL))
    assert result.outcome is EnumServeOutcome.UNINSTALLED
    assert result.served_target == ""
    assert backend.served == {OTHER_PORT: FOREIGN}
    assert ENV_FILE not in backend.env_files
    assert backend.calls.count(f"off {OTHER_PORT}") == 0


def test_the_runtime_runs_the_in_memory_overlay_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "overlays" / "node_tailnet_https_serve_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text((_NODE / "overlay.example.yaml").read_text())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    assert main(["install"]) == 0
    assert "installed" in capsys.readouterr().out
    assert main(["status"]) == 0
